"""Run LIBERO through the UE namespace and collect trial outputs."""
import json
import os
import pwd
import subprocess
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp

from common import POLICY_PORT, ROOT, RUNTIME, UE_DEVICE, command, source_path, sudo, write_file

PRESETS = {"bowl": ("libero_goal", 1), "two-objects": ("libero_10", 0), "drawer": ("libero_10", 3)}


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
            "--connect-timeout", "4", "--max-time", "8"]
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
    env = {"PYTHONPATH": f"{client}:{libero}", "LIBERO_CONFIG_PATH": str(path),
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
        host, port = args.host, args.port
        command("curl", "--noproxy", "*", "--silent", "--show-error", "--fail", "--connect-timeout", "4",
                "--max-time", "8", f"http://{host}:{port}/healthz")
    else:
        probe(site)
        host, port = site["addresses"]["POLICY"], POLICY_PORT

    output_root = Path(args.output).expanduser().absolute()
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(mkdtemp(prefix=f"{datetime.now():%Y%m%d-%H%M%S}-{args.preset}-", dir=output_root))
    print(f"Trial output: {run_dir}", flush=True)
    write_file(run_dir / "condition.json", json.dumps(
        {"suite": suite, "task": task, "seed": args.seed, "init_state": args.init_state,
         "replan_steps": args.replan_steps, "transport": "direct" if args.host else "ue"},
        indent=2) + "\n")
    env["LIBERO_INIT_INDEX"] = str(args.init_state)
    child = ["env", *(f"{key}={value}" for key, value in env.items()), "timeout", "--kill-after=8s",
             f"{args.timeout}s", python, ROOT / "experiments/measure_oai_multitask.py",
             run_dir / "trace.jsonl", ROOT / "sim/main_single.py", "--args.host", host,
             "--args.port", str(port), "--args.task-suite-name", suite, "--args.task-id", str(task),
             "--args.num-trials-per-task", "1", "--args.seed", str(args.seed),
             "--args.replan-steps", str(args.replan_steps), "--args.video-out-path", run_dir]
    command(*(child if args.host else namespace_command(site, child)))
    results = list(run_dir.glob("task*.json"))
    if len(results) != 1:
        raise ValueError(f"expected one LIBERO result in {run_dir}")
    result = json.loads(results[0].read_text())
    if result["error"]:
        raise ValueError(f"rollout raised {result['error']}")
    if not result["success"]:
        print(f"The LIBERO goal was not reached. See {results[0].name}.", flush=True)
        return 2
    print(f"LIBERO goal reached. See {results[0].name}.", flush=True)
    return 0
