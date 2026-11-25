"""Run LIBERO through the UE namespace and collect trial outputs."""
import json
import os
import pwd
import signal
import subprocess
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp

from common import POLICY_PORT, ROOT, RUNTIME, UE_DEVICE, command, source_path, sudo, terminals, write_file
from timing import summarize

PRESETS = {"bowl": ("libero_goal", 1), "two-objects": ("libero_10", 0), "drawer": ("libero_10", 3)}


def ue_site(site, number):
    ues = terminals(site)
    if not 1 <= number <= len(ues):
        raise ValueError(f"UE {number} is outside the configured roster (1..{len(ues)})")
    return site | {"ue_namespace": ues[number - 1]["namespace"]}


def tun_snapshot(site):
    args = ["ip", "-j", "-s", "-4", "addr", "show", "dev", UE_DEVICE]
    entry = json.loads(command(*namespace_command(site, args), capture_output=True, text=True).stdout)[0]
    address = next(info["local"] for info in entry["addr_info"] if info["family"] == "inet")
    stats = entry["stats64"] if "stats64" in entry else entry["stats"]
    return {"namespace": site["ue_namespace"], "device": UE_DEVICE, "ip": address,
            "rx_bytes": stats["rx"]["bytes"], "tx_bytes": stats["tx"]["bytes"]}


def namespace_command(site, child):
    ns = site["ue_namespace"]
    username = os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
    return sudo(["ip", "netns", "exec", ns, "runuser", "-u", username, "--", *child])


def ensure_path(site, key):
    host = site["addresses"][key]
    lookup = ["ip", "-j", "-4", "route", "get", host]
    result = command(*namespace_command(site, lookup), stdout=subprocess.PIPE, text=True)
    if json.loads(result.stdout)[0].get("dev") != UE_DEVICE:
        raise ValueError(f"{host} does not route through {UE_DEVICE}")
    return host


def probe(site, throughput=False):
    host = ensure_path(site, "POLICY")
    http = ["curl", "--noproxy", "*", "--silent", "--show-error", "--fail",
            "--connect-timeout", "4", "--max-time", "8", "--retry", "2", "--retry-delay", "1"]
    command(*namespace_command(site, [*http, f"http://{host}:{POLICY_PORT}/healthz"]))
    print(f"Policy health check passed via {UE_DEVICE}.", flush=True)
    if throughput:
        dn = ensure_path(site, "DN")
        for reverse in ([], ["-R"]):
            command(*namespace_command(site, ["timeout", "20", "iperf3", "-c", dn, "-t", "5", *reverse]))


def environment(site):
    python = Path(os.environ.get("VLA_SIM_ENV_PYTHON", RUNTIME / "sim/venv/bin/python"))
    libero = source_path("libero")
    client = source_path("openpi") / "packages/openpi-client/src"
    if not python.is_file() or not (libero / "libero/libero/init_files").is_dir() or not client.is_dir():
        raise ValueError("prepare the simulation environment and assets first")
    path = RUNTIME / "sim/config"
    data = RUNTIME / "sim/datasets"
    data.mkdir(parents=True, exist_ok=True)
    benchmark = libero / "libero/libero"
    directories = {"benchmark_root": benchmark, "bddl_files": benchmark / "bddl_files",
                   "init_states": benchmark / "init_files", "datasets": data, "assets": benchmark / "assets"}
    write_file(path / "config.yaml", "".join(f"{key}: {json.dumps(str(value))}\n" for key, value in directories.items()))
    env = {"PYTHONPATH": f"{ROOT / 'sim'}:{client}:{libero}", "LIBERO_CONFIG_PATH": str(path),
           "MUJOCO_GL": site["mujoco_gl"], "PYOPENGL_PLATFORM": site["mujoco_gl"],
           "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
           "NO_PROXY": "*", "no_proxy": "*", "PYTHONUNBUFFERED": "1"}
    return python.absolute(), env


def check(site):
    python, env = environment(site)
    command(python, ROOT / "sim/render_check.py", env=os.environ | env)


def run(site, args):
    python, env = environment(site)
    suite, task = PRESETS[args.preset]
    suite = args.suite or suite
    task = args.task if args.task is not None else task
    if args.host:
        before = None
        host, port = args.host, args.port
        command("curl", "--noproxy", "*", "--silent", "--show-error", "--fail", "--connect-timeout", "4",
                "--max-time", "8", f"http://{host}:{port}/healthz")
    else:
        probe(site)
        before = tun_snapshot(site)
        host, port = site["addresses"]["POLICY"], POLICY_PORT

    output_root = Path(args.output).expanduser().absolute()
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(mkdtemp(prefix=f"{datetime.now():%Y%m%d-%H%M%S}-{args.preset}-", dir=output_root))
    print(f"Trial output: {run_dir}", flush=True)
    write_file(run_dir / "condition.json", json.dumps(
        {"suite": suite, "task": task, "seed": args.seed, "init_state": args.init_state,
         "replan_steps": args.replan_steps, "transport": "direct" if args.host else "ue",
         "ue": args.ue if before else None, "clock": args.clock, "renderer": site["mujoco_gl"],
         "tun": before, "max_control_steps": args.max_control_steps,
         "radio_preset": None if args.host else site["radio_preset"]},
        indent=2) + "\n")
    env["LIBERO_INIT_INDEX"] = str(args.init_state)
    env["VLA_ARM_ID"] = str(args.ue)
    if group := os.environ.get("VLA_ARM_GROUP"):
        env["VLA_ARM_GROUP"] = group
    child = ["env", *(f"{key}={value}" for key, value in env.items()), "timeout", "--kill-after=8s",
             f"{args.timeout}s", python, ROOT / "experiments/measure_oai_multitask.py",
             run_dir / "trace.jsonl", ROOT / "sim/main_single.py", "--args.host", host,
             "--args.port", str(port), "--args.task-suite-name", suite, "--args.task-id", str(task),
             "--args.num-trials-per-task", "1", "--args.seed", str(args.seed),
             "--args.replan-steps", str(args.replan_steps), "--args.video-out-path", run_dir,
             "--args.clock", args.clock]
    if args.max_control_steps is not None:
        child.extend(["--args.max-control-steps", str(args.max_control_steps)])
    try:
        command(*(child if args.host else namespace_command(site, child)))
    finally:
        if before:
            after = tun_snapshot(site)
            write_file(run_dir / "user-plane.json", json.dumps({"before": before, "after": after}, indent=2))
    results = list(run_dir.glob("task*.json"))
    if len(results) != 1:
        raise ValueError(f"expected one LIBERO result in {run_dir}")
    result = json.loads(results[0].read_text())
    if result["error"]:
        raise ValueError(f"rollout raised {result['error']}")
    if args.clock == "realtime":
        write_file(run_dir / "timing.json", json.dumps(summarize(run_dir), indent=2))
    if not result["success"]:
        print(f"The LIBERO goal was not reached. See {results[0].name}.", flush=True)
        return 2
    print(f"LIBERO goal reached. See {results[0].name}.", flush=True)
    return 0


def multi(site, site_path, args):
    ues = terminals(site)
    tasks = args.presets or ["bowl"]
    if len(tasks) == 1:
        tasks = tasks * len(ues)
    if len(tasks) != len(ues) or any(task not in PRESETS for task in tasks):
        raise ValueError(f"choose one shared preset or one per UE from {list(PRESETS)}")
    paths = [tun_snapshot(ue_site(site, i)) for i in range(1, len(ues) + 1)]
    if len({p["ip"] for p in paths}) != len(paths):
        raise ValueError("UE PDU addresses must be distinct")
    root = Path(args.output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    group = Path(mkdtemp(prefix=f"{datetime.now():%Y%m%d-%H%M%S}-multi-", dir=root))
    print(f"Multi-UE trial: {group}", flush=True)
    children = []
    start_ns = None
    with ExitStack() as stack:
        try:
            deadline = time.monotonic() + args.timeout
            while True:
                if any(p.poll() is not None for p in children) or time.monotonic() >= deadline:
                    print(f"An arm failed to initialize. See {group}/ue-*.log", flush=True)
                    break
                ready = sum((group / f"ready-{i}").is_file() for i in range(1, len(children) + 1))
                if ready == len(tasks):
                    start_ns = time.monotonic_ns() + 500_000_000
                    write_file(group / "start.ns", str(start_ns))
                    for child in children:
                        child.wait()
                    break
                if len(children) == len(tasks) or len(children) - ready >= args.init_parallelism:
                    time.sleep(0.05)
                    continue
                number = len(children) + 1
                task = tasks[number - 1]
                cmd = [ROOT / "bin/lab", "--site", site_path, "sim", "run", task, "--ue", str(number),
                       "--clock", "realtime", "--output", group / f"ue-{number}"]
                for key in ("seed", "init_state", "replan_steps", "timeout", "suite", "task", "max_control_steps"):
                    value = getattr(args, key)
                    if value is not None:
                        cmd.extend(["--" + key.replace("_", "-"), str(value)])
                log = stack.enter_context((group / f"ue-{number}.log").open("w"))
                children.append(subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                env=os.environ | {"VLA_ARM_GROUP": str(group)}, start_new_session=True))
        finally:
            for p in children:
                if p.poll() is None:
                    try:
                        os.killpg(p.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            for p in children:
                p.wait()
    codes = [p.returncode for p in children] + [None] * (len(tasks) - len(children))
    arms = []
    starts, ends = [], []
    for i, (task, path, code) in enumerate(zip(tasks, paths, codes), 1):
        arm = dict(ue=i, preset=task, namespace=path["namespace"], ip=path["ip"],
                   device=path["device"], exit_code=code)
        for result in (group / f"ue-{i}").glob("*/user-plane.json"):
            arm["run_dir"] = str(result.parent.relative_to(group))
            counters = json.loads(result.read_text())
            for key in ("tx_bytes", "rx_bytes"):
                arm[key] = counters["after"][key] - counters["before"][key]
            timing = result.with_name("timing.json")
            if timing.exists():
                arm["timing"] = json.loads(timing.read_text())
                starts.append(arm["timing"]["first_step_ns"])
                ends.append(arm["timing"]["last_step_end_ns"])
        arms.append(arm)
        outcome = "not started" if code is None else f"exit {code}"
        print(f"UE {i} ({arm['ip']}), {task}: {outcome}", flush=True)
    overlap = max(0, min(ends) - max(starts)) / 1e9 if len(starts) == len(arms) else None
    write_file(group / "summary.json", json.dumps({"start_ns": start_ns, "radio_preset": site["radio_preset"],
               "init_parallelism": args.init_parallelism, "all_arms_overlap_s": overlap, "arms": arms}, indent=2))
    if start_ns is None or any(code not in (0, 2) for code in codes):
        return 1
    return max(codes)
