"""Render runtime-only Compose inputs and manage the two service groups."""
import hashlib
import os
import subprocess
import time
from pathlib import Path

from common import ROOT, RUNTIME, command, lock, render, terminals, write_file
from host import docker_cli
from install import stage_edge


def subscriber_sql(site):
    rows = [render("core/subscriber.sql.in", ue | {"STATIC_IP": f"10.0.0.{10 + ue['slot']}"})
            for ue in terminals(site)]
    return "START TRANSACTION;\n" + "\n".join(rows) + "\nCOMMIT;\n"


def provision(site):
    # Existing MySQL volumes do not run the init scripts again. Preserve AKA sequence numbers.
    compose("core", site, "exec", "-T", "mysql", "sh", "-ec",
            'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql -u "$MYSQL_USER" "$MYSQL_DATABASE"',
            stdin_data=subscriber_sql(site))


def compose(role, site, *args, context=None, stdin_data=None):
    runtime = RUNTIME / role / site["project"]
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    project = f"{site['project']}-{role}"
    addrs = site["addresses"]
    env = {
        "CORE_NETWORK": f"{site['project']}-data",
        "BRIDGE_IF": site["bridge"],
        "BRIDGE_SUBNET": site["bridge_subnet"],
        **{f"{key}_IP": ip for key, ip in addrs.items()},
    }
    with lock(runtime / "compose.lock"):
        if role == "core":
            config = render("core/config.yaml.in", {"DB_PASSWORD": site["db_password"], "IMS_SLOT": addrs["IMS_SLOT"]})
            schema = (ROOT / "core/schema.sql").read_text()
            subscriber = subscriber_sql(site)
            for name, content in (("config.yaml", config), ("schema.sql", schema), ("subscriber.sql", subscriber)):
                # MySQL drops privileges after the directory bind is mounted.
                write_file(runtime / name, content, 0o444)
            expected = hashlib.sha256((site["imsi"] + site["k"] + site["opc"]).encode()).hexdigest()
            env |= {"CORE_RUNTIME": str(runtime), "MYSQL_PASSWORD": site["db_password"],
                    "MYSQL_ROOT_PASSWORD": site["db_root_password"], "SUBSCRIBER_HASH": expected,
                    "CORE_CONFIG_HASH": hashlib.sha256(config.encode()).hexdigest()}
        else:
            cache = Path(os.environ.get("VLA_MODEL_CACHE", RUNTIME / "edge/model-cache")).expanduser().resolve()
            cache.mkdir(parents=True, exist_ok=True)
            env |= {"EDGE_CONTEXT": str(context or RUNTIME / "edge/context"),
                    "POLICY_IMAGE": os.environ.get("VLA_POLICY_IMAGE", f"{site['project']}-openpi:pi05-215abfb"),
                    "MODEL_CACHE": str(cache), "NVIDIA_GPU": site["gpu"],
                    "POLICY_HOST_PORT": str(site["policy_host_port"]),
                    "POLICY_PIP_INDEX": os.environ.get("POLICY_PIP_INDEX", "https://pypi.org/simple"),
                    "POLICY_WHEEL_ORIGIN": os.environ.get("POLICY_WHEEL_ORIGIN", "https://files.pythonhosted.org")}
        compose_env = runtime / "compose.env"
        rows = [key + "='" + value.replace("'", "\\'") + "'\n" for key, value in env.items()]
        write_file(compose_env, "".join(rows))
        command(*docker_cli(), "compose", "--project-name", project, "--env-file", compose_env,
                "-f", ROOT / role / "compose.yaml", *args, env=os.environ | env,
                input=stdin_data, text=True)


def prepare(role, site):
    if role == "core":
        for attempt in range(3):
            try:
                compose("core", site, "pull", "--policy", "missing")
                return
            except subprocess.CalledProcessError:
                if attempt == 2:
                    raise
                time.sleep(3 * (attempt + 1))
    else:
        if "VLA_POLICY_IMAGE" in os.environ:
            command(*docker_cli(), "image", "inspect", os.environ["VLA_POLICY_IMAGE"], "--format", "{{.Id}}")
        else:
            context = stage_edge()
            compose("edge", site, "build", context=context)


def up(role, site, timeout=None):
    wait = timeout or (240 if role == "core" else 2400)
    extra = ["--no-build"] if role == "edge" else []
    compose(role, site, "up", "-d", *extra, "--wait", "--wait-timeout", str(wait))
    if role == "core" and site.get("additional_ues"):
        provision(site)
