"""Run the shared OAI build as gNB, UE or a supervised pair."""
import json
import os
import re
import signal
import subprocess
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp

from common import RUNTIME, UE_DEVICE, command, lock, output, render, status, subscriber_values, write_file
from network import sudo_required, verify_ran


def check_until(condition, seconds, message, children=()):
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        for child in children:
            if child.poll() is not None:
                raise ValueError(f"modem exited with status {child.returncode}; {message}")
        if condition():
            return
        time.sleep(1)
    raise ValueError(message)


def start(args, logs, build, log_name):
    with (logs / log_name).open("wb") as handle:
        return subprocess.Popen([str(arg) for arg in args], cwd=logs, stdin=subprocess.DEVNULL,
                                stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
                                env=os.environ | {"LD_LIBRARY_PATH": str(build)})


def stop(child):
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGINT)
        child.wait(timeout=6)
    except ProcessLookupError:
        child.wait()
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()


def already_running(modem):
    return status("pgrep", "-x", modem) == 0


def run(site, component, profile):
    sudo_required()
    verify_ran(site)
    gnb = component != "ue"
    ue = component != "gnb"
    build = RUNTIME / "ran/build"
    for modem, needed in (("nr-softmodem", gnb), ("nr-uesoftmodem", ue)):
        if needed and not (build / modem).is_file():
            raise ValueError("prepare the RAN build on this host first")

    base = Path("/run") / ("wvla-" + site["project"])
    base.mkdir(mode=0o700, exist_ok=True)
    sock = base / "iq.sock"
    ns = site["ue_namespace"]
    run_dir = RUNTIME / "ran/logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    logs = Path(mkdtemp(prefix=f"{datetime.now():%Y%m%d-%H%M%S}-{component}-", dir=run_dir))
    print(f"Radio logs: {logs}", flush=True)

    with ExitStack() as stack:
        for modem, needed in (("nr-softmodem", gnb), ("nr-uesoftmodem", ue)):
            if needed:
                stack.enter_context(lock(Path(f"/run/lock/wireless-vla-{modem}.lock")))
                if already_running(modem):
                    raise ValueError(f"{modem} already runs on this host")
        children = []
        if gnb:
            config = render("ran/gnb.yaml.in", {"AMF_IP": site["addresses"]["AMF"],
                                              "RAN_TUNNEL_IP": site["ran_tunnel"]})
            if profile == "ul32":
                mask = ",".join(map(str, range(32, 98)))
                config = re.sub(r"(?m)^(- gNB_ID:.*)$", rf'\1\n  ul_prbblacklist: "{mask}"', config)
            if profile == "dl4":
                config = config.replace("MACRLCs:\n- tr_s_preference: local_L1",
                                        "MACRLCs:\n- tr_s_preference: local_L1\n  dl_max_mcs: 4")
            conf = logs / "gnb.yaml"
            write_file(conf, config)
            if sock.is_socket():
                sock.unlink()
            if sock.exists() or sock.is_symlink():
                raise ValueError(f"VRTSIM socket path is occupied: {sock}")
            child = start([build / "nr-softmodem", "-O", conf, "-E", "--device.name", "vrtsim",
                           "--vrtsim.role", "server", "--vrtsim.timescale", "1.0", "--vrtsim.num_ues", "1",
                           "--vrtsim.chanmod", "0", "--vrtsim.cirdb", "0",
                           "--vrtsim.connection_descriptor", sock], logs, build, "gnb.log")
            children.append(child)
            stack.callback(sock.unlink, missing_ok=True)
            stack.callback(stop, child)

        check_until(sock.is_socket, 45, "VRTSIM IPC did not become ready; check gnb.log", children)
        if gnb:
            print("gNB VRTSIM socket is ready.", flush=True)

        if ue:
            names = {line.split()[0] for line in output("ip", "netns", "list").splitlines()}
            if ns not in names:
                command("ip", "netns", "add", ns)
            if set(json_link_names(ns)) != {"lo"}:
                raise ValueError(f"namespace {ns} already has an interface other than lo")
            command("ip", "-n", ns, "link", "set", "lo", "up")
            conf = logs / "ue.yaml"
            write_file(conf, render("ran/ue.yaml.in", subscriber_values(site)))
            child = start(["ip", "netns", "exec", ns, build / "nr-uesoftmodem", "-O", conf,
                           "-E", "-r", "106", "--numerology", "1", "--band", "78", "-C", "3319680000",
                           "--device.name", "vrtsim", "--vrtsim.role", "client", "--vrtsim.ue_id", "0",
                           "--vrtsim.connection_descriptor", sock, "--vrtsim.chanmod", "0",
                           "--vrtsim.cirdb", "0", "--ue-nb-ant-rx", "2", "--ue-nb-ant-tx", "2"],
                          logs, build, "ue.log")
            children.append(child)
            stack.callback(stop, child)
            check_until(lambda: ue_ready(ns), 60, "PDU session did not obtain an IPv4 address; check ue.log", children)
            command("ip", "-n", ns, "link", "set", UE_DEVICE, "mtu", "1300")
            for key in ("POLICY", "DN"):
                command("ip", "-n", ns, "route", "replace", f"{site['addresses'][key]}/32", "dev", UE_DEVICE)
            print(f"UE IPv4 session ready in {ns} on {UE_DEVICE}.", flush=True)

        while all(child.poll() is None for child in children):
            time.sleep(0.5)
        raise ValueError(f"a modem exited; see {logs}")


def ue_ready(namespace):
    result = subprocess.run(["ip", "-n", namespace, "-4", "addr", "show", "dev", UE_DEVICE],
                            capture_output=True, text=True)
    return result.returncode == 0 and "inet " in result.stdout


def json_link_names(namespace):
    return [link["ifname"] for link in json.loads(output("ip", "-n", namespace, "-j", "link", "show"))]
