"""Paths, site validation, and subprocess helpers for the testbed."""
import fcntl
import gzip
import ipaddress
import json
import os
import re
import secrets
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".state"
SITE_DEFAULT = ROOT / "local/site.json"
UE_DEVICE = "oaitun_ue1"
MAX_UES = 16  # MAX_MOBILES_PER_GNB in the pinned OAI build
POLICY_PORT = 8000
# PRBs at 30 kHz, antenna ports, SSB NR-ARFCN and minimum RX-to-TX slots.
RADIO_PRESETS = {"mimo40": (106, 2, 621312, 6), "siso20": (51, 1, 623424, 8),
                 "siso100": (273, 1, 623424, 8)}
DEFAULTS = json.loads((ROOT / "config/site.example.json").read_text())
OFFSETS = {"NRF": 2, "MYSQL": 3, "AMF": 4, "SMF": 5, "UPF": 6, "DN": 7,
           "UDR": 8, "UDM": 9, "AUSF": 10, "IMS_SLOT": 11, "POLICY": 12}


def load_site(path):
    site = json.loads(path.read_text())
    if not isinstance(site, dict):
        raise ValueError("site configuration must be a JSON object")
    secrets = ("imsi", "k", "opc", "db_password", "db_root_password")
    topology = site.get("topology", DEFAULTS["topology"])
    if topology not in ("single", "dual"):
        raise ValueError("topology must be single or dual")
    required = set(secrets)
    if topology == "dual":
        required.update(("ran_ip", "edge_ip"))
    missing = required - site.keys()
    unknown = site.keys() - DEFAULTS.keys() - required - {"additional_ues"}
    if missing or unknown:
        raise ValueError(f"site fields missing {sorted(missing)} or unknown {sorted(unknown)}")
    site = DEFAULTS | site | {"topology": topology}
    if site["radio_preset"] not in RADIO_PRESETS:
        raise ValueError(f"choose radio_preset from {list(RADIO_PRESETS)}")
    underlay = [ipaddress.IPv4Address(site[k]) for k in ("ran_ip", "edge_ip")] if topology == "dual" else []
    tunnel = ipaddress.IPv4Network(site["tunnel_subnet"])
    bridge = ipaddress.IPv4Network(site["bridge_subnet"])
    ue_pool = ipaddress.IPv4Network("10.0.0.0/16")
    if underlay and (underlay[0] == underlay[1] or any(
        ip.is_unspecified or ip.is_loopback or ip.is_multicast for ip in underlay
    )):
        raise ValueError("ran_ip and edge_ip must be distinct, routable underlay IPv4 addresses")
    if tunnel.prefixlen != 30 or not 16 <= bridge.prefixlen <= 28:
        raise ValueError("use a /30 for tunnel_subnet and a /16 through /28 for bridge_subnet")
    if bridge.overlaps(ue_pool) or (topology == "dual" and (
            tunnel.overlaps(bridge) or tunnel.overlaps(ue_pool)
            or any(ip in tunnel or ip in bridge or ip in ue_pool for ip in underlay))):
        raise ValueError("underlay, tunnel, Docker bridge, and UE address ranges must be disjoint")
    for key in ("bridge", "tunnel", "ue_namespace"):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,14}", site[key]):
            raise ValueError(f"{key} must contain 1–15 lowercase letters, digits or hyphens")
    if len({site[k] for k in ("bridge", "tunnel", "ue_namespace")}) != 3:
        raise ValueError("bridge, tunnel and ue_namespace must be distinct")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", site["project"]):
        raise ValueError("project must be a valid Docker Compose project prefix")
    if not re.fullmatch(r"[0-9]+|GPU-[0-9a-fA-F-]+", site["gpu"]):
        raise ValueError("gpu must be an NVIDIA device index or GPU UUID")
    if type(site["policy_host_port"]) is not int or not 1024 <= site["policy_host_port"] <= 65535:
        raise ValueError("policy_host_port must be an integer from 1024 through 65535")
    if site["mujoco_gl"] not in ("egl", "osmesa"):
        raise ValueError("mujoco_gl must be egl or osmesa")
    for key, pattern in (("imsi", r"00101[0-9]{10}"), ("k", r"[0-9a-fA-F]{32}"),
                         ("opc", r"[0-9a-fA-F]{32}"),
                         ("db_password", r"[A-Za-z0-9_-]{16,128}"),
                         ("db_root_password", r"[A-Za-z0-9_-]{16,128}")):
        if not re.fullmatch(pattern, site[key]):
            raise ValueError(f"invalid format for {key}")
    extras = site.get("additional_ues", [])
    if not isinstance(extras, list) or len(extras) >= MAX_UES:
        raise ValueError("invalid additional_ues list")
    identities = {site["imsi"]}
    for ue in extras:
        if (not isinstance(ue, dict) or set(ue) != {"imsi", "k", "opc"}
                or not isinstance(ue["imsi"], str) or not re.fullmatch(r"00101[0-9]{10}", ue["imsi"])
                or any(not isinstance(ue[k], str) or not re.fullmatch(r"[0-9a-fA-F]{32}", ue[k]) for k in ("k", "opc"))
                or ue["imsi"] in identities):
            raise ValueError("additional UEs need distinct IMSIs and valid K/OPc")
        identities.add(ue["imsi"])
    site["bridge_gateway"] = str(bridge.network_address + 1)
    if topology == "dual":
        site["ran_ip"], site["edge_ip"] = map(str, underlay)
        site["ran_tunnel"] = str(tunnel.network_address + 2)
        site["core_tunnel"] = str(tunnel.network_address + 1)
        site["ran_bind"] = site["ran_tunnel"]
    else:
        for key in ("ran_ip", "edge_ip", "tunnel", "tunnel_subnet"):
            site.pop(key)
        site["ran_bind"] = site["bridge_gateway"]
    site["addresses"] = {key: str(bridge.network_address + offset)
                         for key, offset in OFFSETS.items()}
    return site


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(f"another lab process holds {path}") from error
        yield


def write_file(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_text() == text:
        path.chmod(mode)
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w") as temp:
        name = Path(temp.name)
        try:
            temp.write(text)
            temp.close()
            name.chmod(mode)
            name.replace(path)
        finally:
            name.unlink(missing_ok=True)


def render(relative, values):
    text = (ROOT / relative).read_text()
    return re.sub(r"@([A-Z_]+)@", lambda match: values[match[1]], text)


def subscriber_values(site):
    return {"IMSI": site["imsi"], "K": site["k"], "OPC": site["opc"]}


def terminals(site):
    return [dict(slot=slot, namespace=(site["ue_namespace"] if slot == 0 else
                 f"{site['ue_namespace']}-{slot + 1}"), **subscriber_values(ue))
            for slot, ue in enumerate([site, *site.get("additional_ues", [])])]


def extend_ues(data, count):
    extras = data.setdefault("additional_ues", [])
    if not 1 + len(extras) <= count <= MAX_UES:
        raise ValueError("choose the existing UE count or a larger count")
    identities = {data["imsi"], *(ue["imsi"] for ue in extras)}
    while len(identities) < count:
        imsi = f"00101{secrets.randbelow(10**10):010}"
        if imsi not in identities:
            extras.append(dict(imsi=imsi, k=secrets.token_hex(16), opc=secrets.token_hex(16)))
            identities.add(imsi)


def command(*args, **options):
    return subprocess.run([str(arg) for arg in args], check=True, **options)


def output(*args, **options):
    return command(*args, stdout=subprocess.PIPE, text=True, **options).stdout.strip()


def sudo(args):
    return list(args) if os.geteuid() == 0 else ["sudo", *args]


def status(*args):
    return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def unpack_source(name, entry, checkout, stage):
    commit = entry["commit"]
    repository = entry["url"].removeprefix("https://github.com/").removesuffix(".git")
    archive = stage / f"{name}-{commit}.tar.gz"
    if cache := os.environ.get("VLA_SOURCE_ARCHIVES"):
        cached = Path(cache).expanduser() / archive.name
        if cached.is_file():
            archive = cached
    if not archive.is_file():
        command("curl", "--fail", "--location", "--silent", "--show-error", "--retry", "5",
                "--connect-timeout", "15", "--speed-time", "90", "--speed-limit", "1024",
                f"https://codeload.github.com/{repository}/tar.gz/{commit}", "-o", archive)
    with gzip.open(archive, "rb") as handle:
        header = handle.read(2048)
    archived = command("git", "get-tar-commit-id", input=header, stdout=subprocess.PIPE).stdout.decode().strip()
    if archived != commit:
        raise ValueError(f"{archive} does not identify the pinned {name} commit {commit}")
    checkout.mkdir()
    command("tar", "--extract", "--gzip", "--file", archive, "--no-same-owner",
            "--no-same-permissions", "--strip-components=1", "--directory", checkout)
    (checkout / ".archive-commit").write_text(commit + "\n")


def source_path(name):
    cached = RUNTIME / "src" / name
    submodule = ROOT / "vendor" / name
    if (cached / ".archive-commit").is_file() or not (submodule / ".git").exists():
        return cached
    return submodule


def fetch(name):
    submodule = ROOT / "vendor" / name
    archive_supported = name in ("openpi", "libero", "lerobot")
    with lock(RUNTIME / "locks" / f"source-{name}"):
        entry = output("git", "-C", ROOT, "ls-files", "--stage", "--", submodule).split()
        if not entry or entry[0] != "160000":
            raise ValueError(f"missing Git submodule entry: {submodule}")
        commit = entry[1]
        dest = source_path(name)
        archive_commit = dest / ".archive-commit"
        if archive_commit.is_file():
            if archive_commit.read_text().strip() != commit:
                raise ValueError(f"cached {name} does not match {commit}; move aside {dest} and retry")
            return dest
        if (dest / ".git").exists():
            dirty = output("git", "-C", dest, "status", "--porcelain", "-uno", "--ignore-submodules=all")
            if dirty:
                raise ValueError(f"save changes in {dest} before preparing {name}")
            if output("git", "-C", dest, "rev-parse", "HEAD") == commit:
                return dest
            if dest != submodule:
                raise ValueError(f"cached {name} does not match {commit}; move aside {dest} and retry")

        url = output("git", "config", "--file", ROOT / ".gitmodules", "--get", f"submodule.{name}.url")
        if archive_supported:
            skip_git = os.environ.get("VLA_SOURCE_ARCHIVES") or status(
                "curl", "-fsS", "--connect-timeout", "8", "--max-time", "15", "-o", "/dev/null",
                f"{url}/info/refs?service=git-upload-pack")
        else:
            skip_git = False
        if not skip_git:
            env = os.environ | {"GIT_LFS_SKIP_SMUDGE": "1", "GIT_TERMINAL_PROMPT": "0"}
            try:
                command("git", "-C", ROOT, "-c", "http.lowSpeedLimit=1024", "-c", "http.lowSpeedTime=30",
                        "submodule", "update", "--init", "--checkout", "--depth", "1", "--", submodule, env=env)
            except subprocess.CalledProcessError:
                if not archive_supported:
                    raise
            else:
                if output("git", "-C", submodule, "rev-parse", "HEAD") != commit:
                    raise ValueError(f"expected {name} at {commit} under {submodule}")
                return submodule

        dest = RUNTIME / "src" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"Fetching the pinned {name} archive at {commit[:12]}", flush=True)
        with tempfile.TemporaryDirectory(prefix=f"{name}-", dir=dest.parent) as stage:
            checkout = Path(stage) / name
            unpack_source(name, {"commit": commit, "url": url}, checkout, Path(stage))
            checkout.rename(dest)
        return dest
