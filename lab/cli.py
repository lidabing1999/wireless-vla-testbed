"""Single deployment entry point for the two-host VLA testbed."""
import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import containers
import install
import network
import radio
import simulation
from common import DEFAULTS, ROOT, SITE_DEFAULT, command, load_site

HOST_ROLES = ("core", "edge", "ran", "sim")


def bounded(minimum, maximum):
    def parse(value):
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"expected an integer between {minimum} and {maximum}")
        return number
    return parse


def arguments():
    parser = argparse.ArgumentParser(prog="lab", description=__doc__)
    parser.add_argument("--site", type=Path, default=Path(os.environ.get("VLA_SITE", SITE_DEFAULT)),
                        help="local deployment JSON (default: local/site.json)")
    actions = parser.add_subparsers(dest="action", required=True)
    init = actions.add_parser("init", help="create one site configuration with random lab credentials")
    init.add_argument("--ran-ip", required=True)
    init.add_argument("--edge-ip", required=True)
    init.add_argument("--gpu", default=DEFAULTS["gpu"])
    init.add_argument("--bridge-subnet", default=DEFAULTS["bridge_subnet"])
    init.add_argument("--tunnel-subnet", default=DEFAULTS["tunnel_subnet"])
    init.add_argument("--project", default=DEFAULTS["project"])
    init.add_argument("--bridge", default=DEFAULTS["bridge"])
    init.add_argument("--tunnel", default=DEFAULTS["tunnel"])
    init.add_argument("--ue-namespace", default=DEFAULTS["ue_namespace"])
    init.add_argument("--policy-host-port", type=bounded(1024, 65535), default=8000)
    init.add_argument("--mujoco-gl", choices=("egl", "osmesa"), default="egl")
    actions.add_parser("plan", help="show addresses and names without credentials")

    for name, help_text in (("deps", "install OS packages for Ubuntu 22.04"),
                            ("prepare", "fetch dependencies and build or pull the role"),
                            ("doctor", "check role prerequisites")):
        part = actions.add_parser(name, help=help_text)
        part.add_argument("role", choices=HOST_ROLES)
        if name == "prepare":
            part.add_argument("--jobs", type=bounded(1, 64), default=min(4, os.cpu_count() or 2))

    for name, help_text in (("up", "start and wait for Compose health"),
                            ("down", "stop the role and keep its data"),
                            ("status", "list runtime container status"),
                            ("validate", "render and validate Compose configuration")):
        part = actions.add_parser(name, help=help_text)
        part.add_argument("role", choices=("core", "edge"))
        if name == "up":
            part.add_argument("--timeout", type=bounded(1, 7200))

    net = actions.add_parser("network", help="configure IPIP and scoped forwarding on this host")
    net.add_argument("role", choices=("core", "ran"))
    modems = actions.add_parser("radio", help="run the VRTSIM modems in the foreground")
    modems.add_argument("component", choices=("gnb", "ue", "both"), default="both", nargs="?")
    modems.add_argument("--profile", choices=("full", "ul32", "dl4"), default="full")

    sim = actions.add_parser("sim", help="check rendering, check UE path, or run an episode")
    sim_actions = sim.add_subparsers(dest="simulation", required=True)
    sim_actions.add_parser("check", help="render one local frame with no policy server")
    probe = sim_actions.add_parser("probe", help="verify policy HTTP health over the UE TUN")
    probe.add_argument("--throughput", action="store_true", help="also run uplink and downlink iperf3")
    episode = sim_actions.add_parser("run", help="record one LIBERO episode")
    episode.add_argument("preset", choices=simulation.PRESETS, default="bowl", nargs="?")
    episode.add_argument("--suite", choices=("libero_spatial", "libero_object", "libero_goal", "libero_10", "libero_90"))
    episode.add_argument("--task", type=bounded(0, 99))
    episode.add_argument("--init-state", type=bounded(0, 999), default=0)
    episode.add_argument("--seed", type=bounded(0, 2**32 - 1), default=7)
    episode.add_argument("--replan-steps", type=bounded(1, 10), default=5)
    episode.add_argument("--timeout", type=bounded(1, 7200), default=290)
    episode.add_argument("--output", default=str(ROOT / "runs"))
    episode.add_argument("--host", help="use a direct control path for comparison")
    episode.add_argument("--port", type=bounded(1, 65535), default=8000, help="direct endpoint port")
    return parser.parse_args()


def init_site(args):
    path = args.site
    if path.exists():
        raise ValueError(f"{path} already exists")
    data = {key: getattr(args, key) for key in DEFAULTS}
    data.update({"imsi": f"00101{secrets.randbelow(10**10):010}",
                 "k": secrets.token_hex(16), "opc": secrets.token_hex(16),
                 "db_password": secrets.token_hex(20), "db_root_password": secrets.token_hex(20)})
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    try:
        load_site(path)
    except (ValueError, TypeError):
        path.unlink()
        raise
    print(f"Site configuration created at {path}. Copy this file to both hosts.")


def doctor(role):
    install.require_linux()
    programs = {
        "core": ["docker", "ip", "iptables", "modprobe"],
        "edge": ["docker", "git", "nvidia-smi"],
        "ran": ["cmake", "ninja", "gcc-12", "g++-12", "git", "ip", "iptables", "modprobe"],
        "sim": [os.environ.get("VLA_SIM_PYTHON", "python3.10"), "git", "curl", "ip", "timeout"],
    }
    missing = [program for program in programs[role] if not shutil.which(program)]
    if missing:
        raise ValueError(f"missing programs: {', '.join(missing)}")
    if role in ("core", "edge"):
        command("docker", "compose", "version")
        command("docker", "info", "--format", "{{json .Runtimes}}")
    if role == "edge":
        command("nvidia-smi", "--query-gpu=index,uuid,name,memory.free", "--format=csv")
    if role == "ran" and not Path("/dev/net/tun").exists():
        raise ValueError("/dev/net/tun is missing")
    print(f"{role}: prerequisites found.")


def main():
    args = arguments()
    action = args.action
    if action == "init":
        init_site(args)
        return
    if action == "doctor":
        doctor(args.role)
        return
    if action == "deps":
        install.system_deps(args.role)
        return
    if action == "prepare" and args.role in ("ran", "sim"):
        install.require_linux()
        if args.role == "ran":
            install.prepare_ran(args.jobs)
        else:
            install.prepare_sim()
        return

    site = load_site(args.site.expanduser())
    if action == "plan":
        fields = set(DEFAULTS) | {"ran_tunnel", "core_tunnel", "addresses"}
        print(json.dumps({key: value for key, value in site.items() if key in fields}, indent=2))
        return
    install.require_linux()
    if action == "prepare":
        containers.prepare(args.role, site)
    elif action == "validate":
        containers.compose(args.role, site, "config", "--quiet")
        print(f"{args.role}: Compose configuration valid.")
    elif action == "up":
        wait = args.timeout or (240 if args.role == "core" else 2400)
        if args.role == "edge":
            containers.compose("edge", site, "up", "-d", "--no-build", "--wait", "--wait-timeout", str(wait))
        else:
            containers.compose("core", site, "up", "-d", "--wait", "--wait-timeout", str(wait))
    elif action == "down":
        containers.compose(args.role, site, "down")
    elif action == "status":
        containers.compose(args.role, site, "ps", "-a")
    elif action == "network":
        network.apply(args.role, site)
    elif action == "radio":
        if args.component == "ue" and args.profile != "full":
            raise ValueError("set the resource profile on the gNB process")
        signal.signal(signal.SIGTERM, lambda _signal, _frame: signal.raise_signal(signal.SIGINT))
        radio.run(site, args.component, args.profile)
    elif args.simulation == "check":
        simulation.check(site)
    elif args.simulation == "probe":
        simulation.probe(site, args.throughput)
    else:
        return simulation.run(site, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, TypeError, OSError) as error:
        sys.exit(f"lab: {error}")
    except subprocess.CalledProcessError as error:
        sys.exit(f"lab: {error.cmd[0]} exited with status {error.returncode}")
    except KeyboardInterrupt:
        sys.exit(130)
