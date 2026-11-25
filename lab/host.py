"""Prepare the container engine and NVIDIA runtime on a fresh Ubuntu host."""
import json
import os
import shutil
import tempfile
from urllib.parse import urlsplit
from pathlib import Path

from common import command, output, status, sudo

DOCKER_PACKAGES = ("docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin")


def require_ubuntu():
    if not Path("/run/systemd/system").is_dir():
        raise ValueError("deploy requires an Ubuntu host with systemd as its service manager")
    info = dict(line.strip().split("=", 1) for line in Path("/etc/os-release").read_text().splitlines()
                if "=" in line and not line.startswith("#"))
    if (info.get("ID", "").strip('"'), info.get("VERSION_ID", "").strip('"')) != ("ubuntu", "22.04"):
        raise ValueError("the automatic host installer supports Ubuntu 22.04 x86_64")


def docker_cli():
    sock = Path("/var/run/docker.sock")
    if os.environ.get("DOCKER_HOST") or os.environ.get("DOCKER_CONTEXT") or os.access(sock, os.W_OK):
        return ["docker"]
    return sudo(["docker"])


def apt(*packages):
    command(*sudo(["apt-get", "install", "-y", *packages]))


def apt_repo(name, key_url, source, suffix):
    keyring = f"/etc/apt/keyrings/{name}.{suffix}"
    source_path = f"/etc/apt/sources.list.d/{name}.sources" if name == "docker" else f"/etc/apt/sources.list.d/{name}.list"
    with tempfile.TemporaryDirectory(prefix=f"vla-apt-{name}-") as tmp:
        tmp = Path(tmp)
        key = tmp / "key"
        command("curl", "-fsSL", "--retry", "5", key_url, "-o", key)
        if suffix == "gpg":
            binary = tmp / "key.gpg"
            command("gpg", "--batch", "--dearmor", "--output", binary, key)
            key = binary
        definition = tmp / "repository"
        definition.write_text(source.replace("@KEYRING@", keyring))
        command(*sudo(["install", "-d", "-m", "0755", "/etc/apt/keyrings"]))
        command(*sudo(["install", "-m", "0644", key, keyring]))
        command(*sudo(["install", "-m", "0644", definition, source_path]))
    command(*sudo(["apt-get", "update"]))


def prepare_docker():
    if shutil.which("docker") and status("docker", "compose", "version") == 0:
        if status(*docker_cli(), "info") == 0:
            return
        command(*sudo(["systemctl", "enable", "--now", "docker.service"]))
        command(*docker_cli(), "info", "--format", "{{.ServerVersion}}")
        return

    if shutil.which("docker"):
        apt("docker-compose-plugin" if status("dpkg-query", "-W", "docker-ce-cli") == 0 else "docker-compose-v2")
    else:
        apt_repo("docker", "https://download.docker.com/linux/ubuntu/gpg",
                 "Types: deb\nURIs: https://download.docker.com/linux/ubuntu\n"
                 "Suites: jammy\nComponents: stable\nArchitectures: amd64\nSigned-By: @KEYRING@\n", "asc")
        apt(*DOCKER_PACKAGES)
    command(*sudo(["systemctl", "enable", "--now", "docker.service"]))
    command("docker", "compose", "version")
    command(*docker_cli(), "info", "--format", "{{.ServerVersion}}")


def detect_nvidia():
    if shutil.which("nvidia-smi") and status("nvidia-smi", "-L") == 0:
        return True
    for device in Path("/sys/bus/pci/devices").glob("*"):
        if ((device / "vendor").read_text().strip() == "0x10de"
                and (device / "class").read_text().strip().startswith("0x03")):
            return True
    return False


def prepare_gpu(gpu):
    if not detect_nvidia():
        raise ValueError("OpenPI pi0.5 requires an NVIDIA GPU visible to this host")
    if not shutil.which("nvidia-smi") or status("nvidia-smi", "-L"):
        apt("ubuntu-drivers-common")
        command(*sudo(["ubuntu-drivers", "install", "--gpgpu"]))
        if not shutil.which("nvidia-smi") or status("nvidia-smi", "-L"):
            raise ValueError("NVIDIA driver installed; reboot to load it, then run the same deploy command")
    command("nvidia-smi", "-i", gpu, "--query-gpu=name,driver_version,memory.total", "--format=csv")
    if '"nvidia"' in output(*docker_cli(), "info", "--format", "{{json .Runtimes}}"):
        return
    if output(*docker_cli(), "ps", "-q"):
        raise ValueError("enabling NVIDIA in Docker needs a daemon restart; stop existing workloads first")
    if not shutil.which("nvidia-ctk"):
        apt_repo("nvidia-container-toolkit", "https://nvidia.github.io/libnvidia-container/gpgkey",
                 "deb [signed-by=@KEYRING@] https://nvidia.github.io/libnvidia-container/stable/deb/amd64 /\n",
                 "gpg")
        apt("nvidia-container-toolkit")
    command(*sudo(["nvidia-ctk", "runtime", "configure", "--runtime=docker"]))
    command(*sudo(["systemctl", "restart", "docker.service"]))
    if '"nvidia"' not in output(*docker_cli(), "info", "--format", "{{json .Runtimes}}"):
        raise ValueError("Docker did not register the NVIDIA runtime")


def configure_registry_mirror():
    mirror = os.environ.get("VLA_DOCKER_MIRROR")
    if not mirror:
        return
    mirror = mirror.rstrip("/")
    url = urlsplit(mirror)
    if url.scheme != "https" or not url.hostname or url.username or url.query or url.fragment:
        raise ValueError("VLA_DOCKER_MIRROR must be an HTTPS registry mirror URL without credentials")
    path = Path("/etc/docker/daemon.json")
    data = json.loads(output(*sudo(["cat", path]))) if path.exists() else {}
    mirrors = data.setdefault("registry-mirrors", [])
    if mirror in mirrors:
        return
    if output(*docker_cli(), "ps", "-q"):
        raise ValueError("configuring a Docker mirror requires a restart; stop existing workloads first")
    mirrors.append(mirror)
    with tempfile.TemporaryDirectory(prefix="vla-docker-") as tmp:
        config = Path(tmp) / "daemon.json"
        config.write_text(json.dumps(data, indent=2) + "\n")
        config.chmod(0o600)
        command(*sudo(["dockerd", "--validate", "--config-file", config]))
        command(*sudo(["install", "-d", "-m", "0755", path.parent]))
        command(*sudo(["install", "-m", "0600", config, path]))
    command(*sudo(["systemctl", "restart", "docker.service"]))
