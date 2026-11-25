"""Configure the N2/N3 IPIP link and the limited Docker forwarding rules."""
import json
import os
import socket
import subprocess
from pathlib import Path

from common import command, lock, output, status


def sudo_required():
    if os.geteuid() != 0:
        raise ValueError("this command needs sudo on the target Linux host")


def links(interface):
    result = subprocess.run(["ip", "-j", "-d", "link", "show", "dev", interface],
                            capture_output=True, text=True)
    return json.loads(result.stdout) if result.returncode == 0 else []


def route(destination, namespace=None):
    args = ["ip", "-j", "-4", "route", "get", destination]
    if namespace:
        args = ["ip", "netns", "exec", namespace, *args]
    return json.loads(output(*args))[0]


def iptables(chain, rule, table="filter"):
    tool = ["iptables", "-w", "-t", table]
    if status(*tool, "-C", chain, *rule):
        command(*tool, "-I", chain, "1", *rule)


def apply(role, site):
    sudo_required()
    if site["topology"] == "single":
        try:
            socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_SCTP).close()
        except OSError:
            command("modprobe", "sctp")
        if not Path("/dev/net/tun").is_char_device():
            command("modprobe", "tun")
        verify_ran(site)
        return
    interface = site["tunnel"]
    edge = role == "core"
    here, peer = (site["edge_ip"], site["ran_ip"]) if edge else (site["ran_ip"], site["edge_ip"])
    inside = site["core_tunnel"] if edge else site["ran_tunnel"]
    bridge = site["bridge"]
    addrs = site["addresses"]
    with lock(Path(f"/run/lock/{interface}.lock")):
        if edge:
            attached = links(bridge)
            if not attached or attached[0].get("linkinfo", {}).get("info_kind") != "bridge":
                raise ValueError("start the core Compose group before applying core network settings")
            command("iptables", "-w", "-S", "DOCKER-USER", stdout=subprocess.DEVNULL)
        else:
            for address in (addrs["AMF"], addrs["UPF"]):
                exact = json.loads(output("ip", "-j", "-4", "route", "show", "exact", f"{address}/32"))
                if any(entry.get("dev") != interface for entry in exact):
                    raise ValueError(f"{address}/32 already uses a different interface")
        command("ip", "-4", "route", "get", peer, "from", here, stdout=subprocess.DEVNULL)
        command("modprobe", "ipip")
        command("modprobe", "sctp")
        existing = links(interface)
        if existing:
            info = existing[0].get("linkinfo", {})
            data = info.get("info_data", {})
            if info.get("info_kind") != "ipip" or (data.get("local"), data.get("remote")) != (here, peer):
                raise ValueError(f"{interface} already exists with different tunnel settings")
            assigned = json.loads(output("ip", "-j", "-4", "addr", "show", "dev", interface))
            addresses = [(a["local"], a["prefixlen"]) for a in assigned[0].get("addr_info", [])]
            if addresses and addresses != [(inside, 30)]:
                raise ValueError(f"{interface} already has different IPv4 addresses")
        else:
            command("ip", "tunnel", "add", interface, "mode", "ipip", "local", here,
                    "remote", peer, "ttl", "64")
        command("ip", "addr", "replace", f"{inside}/30", "dev", interface)
        command("ip", "link", "set", "dev", interface, "mtu", "1400", "up")
        iptables("INPUT", ["-p", "4", "-s", peer, "-d", here, "-j", "ACCEPT"])
        if edge:
            enable_forwarding(site)
        else:
            for address in (addrs["AMF"], addrs["UPF"]):
                command("ip", "route", "replace", f"{address}/32", "via", site["core_tunnel"], "dev", interface)
        print(f"{role}: {interface} {inside}/30 is up.", flush=True)


def enable_forwarding(site):
    if output("sysctl", "-n", "net.ipv4.ip_forward") != "1":
        command("sysctl", "-w", "net.ipv4.ip_forward=1")
    tunnel, bridge, peer = site["tunnel"], site["bridge"], site["ran_tunnel"]
    for key, proto, port in (("AMF", "sctp", "38412"), ("UPF", "udp", "2152")):
        target = site["addresses"][key]
        iptables("DOCKER-USER", ["-i", tunnel, "-o", bridge, "-s", peer, "-d", target,
                                 "-p", proto, "--dport", port, "-j", "ACCEPT"])
        reply = ["-o", tunnel, "-s", target, "-d", peer, "-p", proto, "--sport", port]
        iptables("DOCKER-USER", ["-i", bridge, *reply, "-j", "ACCEPT"])
        # Preserve the advertised AMF and UPF addresses on packets toward the gNB.
        iptables("POSTROUTING", [*reply, "-j", "ACCEPT"], table="nat")


def verify_ran(site):
    device = site["bridge"] if site["topology"] == "single" else site["tunnel"]
    for key in ("AMF", "UPF"):
        entry = route(site["addresses"][key])
        if entry.get("dev") != device or entry.get("prefsrc") != site["ran_bind"]:
            if site["topology"] == "single":
                raise ValueError(f"{key} is not reachable on {device}; start the local core")
            raise ValueError(f"{key} is not routed through {device}; run lab network ran")
