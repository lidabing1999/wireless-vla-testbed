"""Deploy the wireless VLA testbed on one or two Linux hosts."""
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
import deploy
import host
import install
import network
import radio
import simulation
from common import DEFAULTS, MAX_UES, RADIO_PRESETS, ROOT, SITE_DEFAULT, command, extend_ues, load_site, lock, terminals, write_file

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
    init.add_argument("--topology", choices=("single", "dual"))
    init.add_argument("--ran-ip")
    init.add_argument("--edge-ip")
    init.add_argument("--gpu", default=DEFAULTS["gpu"])
    init.add_argument("--bridge-subnet", default=DEFAULTS["bridge_subnet"])
    init.add_argument("--tunnel-subnet", default=DEFAULTS["tunnel_subnet"])
    init.add_argument("--project", default=DEFAULTS["project"])
    init.add_argument("--bridge", default=DEFAULTS["bridge"])
    init.add_argument("--tunnel", default=DEFAULTS["tunnel"])
    init.add_argument("--ue-namespace", default=DEFAULTS["ue_namespace"])
    init.add_argument("--policy-host-port", type=bounded(1024, 65535), default=8000)
    init.add_argument("--mujoco-gl", choices=("egl", "osmesa"))
    init.add_argument("--ue-count", type=bounded(1, MAX_UES), default=1)
    init.add_argument("--radio-preset", choices=RADIO_PRESETS, default=DEFAULTS["radio_preset"])
    expand = actions.add_parser("ues", help="extend the UE roster in an existing private site file")
    expand.add_argument("count", type=bounded(1, MAX_UES))
    expand.add_argument("--radio-preset", choices=RADIO_PRESETS)
    actions.add_parser("subscribers", help="apply the site subscriptions to the running core database")
    actions.add_parser("plan", help="show addresses and names without credentials")
    deployment = actions.add_parser("deploy", help="install, build and start from a clean Ubuntu host")
    deployment.add_argument("role", choices=("all", *HOST_ROLES), nargs="?", default="all")
    deployment.add_argument("--gpu", help="GPU device for a new single-host site")
    deployment.add_argument("--jobs", type=bounded(1, 64), default=min(4, os.cpu_count() or 2))
    deployment.add_argument("--smoke", action="store_true", help="run the bowl task through the UE")

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
        part.add_argument("role", choices=("core", "edge", "ran") if name != "validate" else ("core", "edge"))
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
    probe.add_argument("--ue", type=bounded(1, MAX_UES), default=1)
    episode = sim_actions.add_parser("run", help="record one LIBERO episode")
    episode.add_argument("preset", choices=simulation.PRESETS, default="bowl", nargs="?")
    episode.add_argument("--ue", type=bounded(1, MAX_UES), default=1)
    episode.add_argument("--clock", choices=("blocking", "realtime"), default="blocking")
    multi = sim_actions.add_parser("multi", help="run one 20 Hz LIBERO environment per UE")
    multi.add_argument("presets", nargs="*", metavar="TASK", help="one shared task or one task per UE")
    multi.add_argument("--init-parallelism", type=bounded(1, MAX_UES), default=2,
                       help="maximum simultaneous environment initializations before the shared start")
    for target in (multi, episode):
        episode_options(target)
    episode.add_argument("--host", help="use a direct control path for comparison")
    episode.add_argument("--port", type=bounded(1, 65535), default=8000, help="direct endpoint port")
    return parser.parse_args()


def episode_options(episode):
    episode.add_argument("--max-control-steps", type=bounded(1, 10000),
                         help="episode horizon after warmup, including all hold steps (default: suite horizon)")
    episode.add_argument("--suite", choices=("libero_spatial", "libero_object", "libero_goal", "libero_10", "libero_90"))
    episode.add_argument("--task", type=bounded(0, 99))
    episode.add_argument("--init-state", type=bounded(0, 999), default=0)
    episode.add_argument("--seed", type=bounded(0, 2**32 - 1), default=7)
    episode.add_argument("--replan-steps", type=bounded(1, 10), default=5)
    episode.add_argument("--timeout", type=bounded(1, 7200), default=290)
    episode.add_argument("--output", default=str(ROOT / "runs"))


def init_site(path, overrides, count=1):
    path = path.expanduser()
    if path.exists():
        raise ValueError(f"{path} already exists")
    data = DEFAULTS | overrides
    if not overrides.get("mujoco_gl"):
        data["mujoco_gl"] = "osmesa" if data["topology"] == "single" else "egl"
    if data["topology"] == "single":
        for key in ("ran_ip", "edge_ip", "tunnel", "tunnel_subnet"):
            data.pop(key, None)
    data.update({"imsi": f"00101{secrets.randbelow(10**10):010}",
                 "k": secrets.token_hex(16), "opc": secrets.token_hex(16),
                 "db_password": secrets.token_hex(20), "db_root_password": secrets.token_hex(20)})
    extend_ues(data, count)
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
    print(f"Site configuration created at {path}.")
    if data["topology"] == "dual":
        print("Copy this file to both hosts.")


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
        command(*host.docker_cli(), "info", "--format", "{{json .Runtimes}}")
    if role == "edge":
        command("nvidia-smi", "--query-gpu=index,uuid,name,memory.free", "--format=csv")
    if role == "ran" and not Path("/dev/net/tun").exists():
        raise ValueError("/dev/net/tun is missing")
    print(f"{role}: prerequisites found.")


def main():
    args = arguments()
    action = args.action
    if action == "init":
        topology = args.topology or ("dual" if args.ran_ip or args.edge_ip else "single")
        if topology == "dual" and not (args.ran_ip and args.edge_ip):
            raise ValueError("a dual-host site requires both --ran-ip and --edge-ip")
        values = {key: getattr(args, key) for key in DEFAULTS}
        init_site(args.site, values | {"topology": topology}, args.ue_count)
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

    site_path = args.site.expanduser().absolute()
    if action == "deploy" and not site_path.exists():
        install.require_linux()
        host.require_ubuntu()
        init_site(site_path, {"topology": "single", "gpu": args.gpu or DEFAULTS["gpu"]})
    site = load_site(site_path)
    if action == "ues":
        with lock(site_path.with_suffix(".lock")):
            data = json.loads(site_path.read_text())
            extend_ues(data, args.count)
            if args.radio_preset:
                data["radio_preset"] = args.radio_preset
            write_file(site_path, json.dumps(data, indent=2) + "\n")
        print(f"Site configured for {args.count} UEs. Copy this private file to the other host.")
        return
    if action == "plan":
        fields = set(DEFAULTS) | {"ran_tunnel", "core_tunnel", "addresses", "ran_bind", "bridge_gateway"}
        plan = {key: value for key, value in site.items() if key in fields}
        plan["ue_namespaces"] = [ue["namespace"] for ue in terminals(site)]
        print(json.dumps(plan, indent=2))
        return
    install.require_linux()
    if action == "subscribers":
        return containers.provision(site)
    if action == "deploy":
        if args.gpu is not None and site["gpu"] != args.gpu:
            raise ValueError("edit gpu in the existing site configuration to change devices")
        return deploy.deploy(site, site_path, args.role, args.jobs, args.smoke)
    if action == "prepare":
        containers.prepare(args.role, site)
    elif action == "validate":
        containers.compose(args.role, site, "config", "--quiet")
        print(f"{args.role}: Compose configuration valid.")
    elif action == "up":
        if args.role == "ran":
            deploy.systemd_radio("start", site, site_path, args.timeout or 120)
        else:
            containers.up(args.role, site, args.timeout)
    elif action == "down":
        if args.role == "ran":
            deploy.systemd_radio("stop", site)
        else:
            containers.compose(args.role, site, "down")
    elif action == "status":
        if args.role == "ran":
            command("systemctl", "status", f"{site['project']}-radio.service", "--no-pager")
        else:
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
    elif args.simulation == "multi":
        signal.signal(signal.SIGTERM, lambda _signal, _frame: signal.raise_signal(signal.SIGINT))
        return simulation.multi(site, site_path, args)
    elif args.simulation == "probe":
        simulation.probe(simulation.ue_site(site, args.ue), args.throughput)
    else:
        return simulation.run(simulation.ue_site(site, args.ue), args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, TypeError, OSError) as error:
        sys.exit(f"lab: {error}")
    except subprocess.CalledProcessError as error:
        sys.exit(f"lab: {error.cmd[0]} exited with status {error.returncode}")
    except KeyboardInterrupt:
        sys.exit(130)
