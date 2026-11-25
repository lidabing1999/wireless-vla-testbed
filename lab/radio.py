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

from common import RADIO_PRESETS, RUNTIME, UE_DEVICE, command, lock, output, render, terminals, write_file
from network import sudo_required, verify_ran

# Kernel tunnel modules can create down fallback devices in every new netns.
FALLBACK_TUNNELS = {"tunl0": "ipip", "sit0": "sit", "gre0": "gre", "gretap0": "gretap",
                    "erspan0": "erspan", "ip6tnl0": "ip6tnl", "ip6gre0": "ip6gre"}


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
    processes = subprocess.run(["ps", "-C", modem, "-o", "stat="], capture_output=True, text=True)
    return any(not state.startswith("Z") for state in processes.stdout.split())


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
    core_ready = base / "ngap-ready"
    ues = terminals(site)
    prbs, ports, ssb, min_rx_tx = RADIO_PRESETS[site["radio_preset"]]
    center_hz = 3_000_000_000 + (ssb - 600_000) * 15_000
    # Type-1 RIV for the full BWP starting at RB 0 on a 275-RB reference grid.
    bwp = 275 * (prbs - 1) if prbs <= 138 else 275 * (276 - prbs) + 274
    # For 273 PRBs OAI's native rate is 122.88 MS/s; -E would increase it.
    sampling = [] if prbs == 273 else ["-E"]
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
            core_ready.unlink(missing_ok=True)
            slot_config = f"  - antennas: {ports}x{ports}\n    model_id: 0\n    ds_ns: 1.0\n    speed_mps: 1.5"
            config = render("ran/gnb.yaml.in", {"AMF_IP": site["addresses"]["AMF"],
                                              "RAN_BIND_IP": site["ran_bind"], "UE_COUNT": str(len(ues)),
                                              "UE_CONFIG": "\n".join(slot_config for _ in ues),
                                              "PORTS": str(ports), "PRBS": str(prbs), "BWP": str(bwp),
                                              "SSB": str(ssb), "POINT_A": str(ssb - 12 * prbs),
                                              "RX_TX_TIME": str(min_rx_tx),
                                              "SLOT_AHEAD": "8" if prbs == 273 else "6"})
            if profile == "ul32":
                mask = ",".join(map(str, range(32, prbs - 8)))
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
            child = start([build / "nr-softmodem", "-O", conf, *sampling, "--device.name", "vrtsim",
                           "--vrtsim.role", "server", "--vrtsim.timescale", "1.0", "--vrtsim.num_ues", str(len(ues)),
                           "--vrtsim.chanmod", "0", "--vrtsim.cirdb", "0",
                           "--vrtsim.connection_descriptor", sock], logs, build, "gnb.log")
            children.append(child)
            stack.callback(sock.unlink, missing_ok=True)
            stack.callback(stop, child)

        check_until(sock.is_socket, 45, "VRTSIM IPC did not become ready; check gnb.log", children)
        if gnb:
            gnb_log = logs / "gnb.log"
            check_until(lambda: b"Received NGSetupResponse from AMF" in gnb_log.read_bytes(),
                        75, "gNB did not complete NG setup with the AMF; check gnb.log", children)
            write_file(core_ready, str(sock.stat().st_ino))
            stack.callback(core_ready.unlink, missing_ok=True)
            print("gNB VRTSIM and AMF association are ready.", flush=True)

        if ue:
            if not gnb:
                check_until(lambda: core_ready.is_file() and core_ready.read_text() == str(sock.stat().st_ino),
                            75, "the gNB has not reported a ready AMF association")
            names = {line.split()[0] for line in output("ip", "netns", "list").splitlines()}
            for terminal in ues:
                ns = terminal["namespace"]
                if ns not in names:
                    command("ip", "netns", "add", ns)
                    stack.callback(remove_unused_namespace, ns)
                if has_active_or_custom_links(ns):
                    raise ValueError(f"namespace {ns} already contains another configured interface")
                command("ip", "-n", ns, "link", "set", "lo", "up")
                ue_logs = logs if len(ues) == 1 else logs / f"ue-{terminal['slot'] + 1}"
                ue_logs.mkdir(exist_ok=True)
                conf = ue_logs / "ue.yaml"
                write_file(conf, render("ran/ue.yaml.in", terminal | {"SLOT": str(terminal["slot"])}))
                child = start(["ip", "netns", "exec", ns, build / "nr-uesoftmodem", "-O", conf,
                               *sampling, "-r", str(prbs), "--numerology", "1", "--band", "78", "-C", str(center_hz),
                               "--ssb", str(6 * prbs - 120),
                               "--device.name", "vrtsim", "--vrtsim.role", "client", "--vrtsim.ue_id", str(terminal["slot"]),
                               "--vrtsim.connection_descriptor", sock, "--vrtsim.chanmod", "0",
                               "--vrtsim.cirdb", "0", "--ue-nb-ant-rx", ports, "--ue-nb-ant-tx", ports],
                              ue_logs, build, "ue.log")
                children.append(child)
                stack.callback(stop, child)
                # Complete random access before the next modem starts on this cell.
                check_until(lambda: ue_ready(ns), 90, f"{ns} did not obtain a PDU address; see {ue_logs}/ue.log", children)
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


def has_active_or_custom_links(namespace):
    links = json.loads(output("ip", "-n", namespace, "-j", "-d", "link", "show"))
    for link in links:
        name = link["ifname"]
        if name != "lo" and (name not in FALLBACK_TUNNELS or "UP" in link["flags"]
                             or link.get("linkinfo", {}).get("info_kind") != FALLBACK_TUNNELS[name]):
            return True
    return False


def remove_unused_namespace(namespace):
    if not output("ip", "netns", "pids", namespace):
        command("ip", "netns", "delete", namespace)
