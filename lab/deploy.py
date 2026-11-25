"""Install and start the selected components, using the same per-role entry points."""
import json
import os
import subprocess
import time

import containers
import host
import install
import simulation
from common import ROOT, RUNTIME, UE_DEVICE, command, lock, status, sudo, terminals


def policy_route_ready(namespace, destination):
    # Match the lookup made by the application. Forcing oif can succeed before
    # the supervisor has installed the policy host route in the last namespace.
    result = subprocess.run(sudo(["ip", "-n", namespace, "-j", "-4", "route", "get", destination]),
                            capture_output=True, text=True)
    return result.returncode == 0 and any(row.get("dev") == UE_DEVICE and row.get("prefsrc")
                                         for row in json.loads(result.stdout))


def systemd_radio(action, site, site_path=None, timeout=120):
    unit = f"{site['project']}-radio.service"
    if action == "stop":
        # A collected transient unit can be absent after a clean stop or reboot.
        if status("systemctl", "is-active", "--quiet", unit) == 0:
            command(*sudo(["systemctl", "stop", unit]))
        return
    if status("systemctl", "is-active", "--quiet", unit):
        command(*sudo(["systemd-run", f"--unit={unit}", "--collect", "--service-type=exec",
                       "--property=KillMode=mixed", "--property=TimeoutStopSec=20s",
                       "--", ROOT / "bin/lab", "--site", site_path, "radio", "both"]))

    def invocation():
        return subprocess.run(["systemctl", "show", "-p", "InvocationID", "--value", unit],
                              capture_output=True, text=True).stdout.strip()

    started = invocation()
    deadline = time.monotonic() + timeout
    while started and time.monotonic() < deadline and invocation() == started:
        if status("systemctl", "is-active", "--quiet", unit):
            break
        if all(policy_route_ready(ue["namespace"], site["addresses"]["POLICY"]) for ue in terminals(site)):
            print(f"Radio service ready: {unit}", flush=True)
            return
        time.sleep(2)
    raise ValueError(f"radio did not become ready; see sudo journalctl -u {unit} and .state/ran/logs")


def deploy(site, site_path, role, jobs, smoke):
    install.require_linux()
    host.require_ubuntu()
    if role == "all" and site["topology"] != "single":
        raise ValueError("deploy all uses a single-host site; deploy each role on its host for a dual site")
    if smoke and role != "all":
        raise ValueError("--smoke requires deploy all")
    roles = ("core", "edge", "ran", "sim") if role == "all" else (role,)
    if "edge" in roles and not host.detect_nvidia():
        raise ValueError("OpenPI pi0.5 requires an NVIDIA GPU visible to this host")
    if os.geteuid() != 0 and status("sudo", "-n", "true"):
        command("sudo", "-v")

    with lock(RUNTIME / "locks/deploy"):
        install.system_deps(*roles)
        if set(roles) & {"core", "edge"}:
            host.prepare_docker()
            host.configure_registry_mirror()
        if "edge" in roles:
            host.prepare_gpu(site["gpu"])
        if "ran" in roles:
            install.prepare_ran(jobs)
        if "sim" in roles:
            install.prepare_sim()
            simulation.check(site)
        if "core" in roles:
            containers.prepare("core", site)
            containers.up("core", site)
            if site["topology"] == "dual":
                command(*sudo([ROOT / "bin/lab", "--site", site_path, "network", "core"]))
        if "edge" in roles:
            containers.prepare("edge", site)
            containers.up("edge", site)
        if "ran" in roles:
            command(*sudo([ROOT / "bin/lab", "--site", site_path, "network", "ran"]))
            systemd_radio("start", site, site_path)
        if role == "all":
            if smoke:
                return subprocess.run([ROOT / "bin/lab", "--site", site_path, "sim", "run", "bowl"]).returncode
            simulation.probe(site)
