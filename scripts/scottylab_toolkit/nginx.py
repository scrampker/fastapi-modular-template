"""Declarative edits to nginx-certs.yml and nginx-vhosts.yml.

Every scotty host that accepts LAN or WAN traffic terminates at the nginx
LXC, so these two files are the source of truth for "what zones serve
traffic" and "what fqdn routes where." Changes here are committed to
scottylab; the actual nginx config rolls out via the corresponding ansible
playbooks (`workloads/nginx-certs.yml`, `workloads/nginx-vhosts.yml`).
"""

import re

from .paths import SCOTTYLAB_WORKLOADS
from .yaml_inserts import append_to_yaml_list

CERTS_YML = SCOTTYLAB_WORKLOADS / "nginx-certs.yml"
VHOSTS_YML = SCOTTYLAB_WORKLOADS / "nginx-vhosts.yml"


def add_cert_apex(apex: str) -> bool:
    """Append `- apex: <apex>` to the nginx_certs list. No-op if present."""
    if not CERTS_YML.exists():
        print(f"  nginx-certs.yml not found at {CERTS_YML}")
        return False
    text = CERTS_YML.read_text()
    if re.search(rf"^\s*-\s*apex:\s*{re.escape(apex)}\s*$", text, re.M):
        print(f"  {apex} already in nginx-certs.yml")
        return False
    if append_to_yaml_list(CERTS_YML, "nginx_certs",
                           [f"      - apex: {apex}"]):
        print(f"  Added apex '{apex}' to nginx-certs.yml")
        return True
    print(f"  Could not locate nginx_certs: block")
    return False


def add_vhost(name: str, server_name: str, cert_apex: str,
              upstream_host_or_ip: str, port: int) -> bool:
    """Append a vhost entry to nginx-vhosts.yml. No-op if present."""
    if not VHOSTS_YML.exists():
        print(f"  nginx-vhosts.yml not found at {VHOSTS_YML}")
        return False
    text = VHOSTS_YML.read_text()
    if re.search(rf"^\s*-\s*name:\s*{re.escape(name)}\s*$", text, re.M):
        print(f"  vhost '{name}' already in nginx-vhosts.yml")
        return False

    entry_lines = [
        "",
        f"      - name: {name}",
        f"        server_name: {server_name}",
        f"        cert: {cert_apex}",
        f"        upstream: http://{upstream_host_or_ip}:{port}",
    ]
    if append_to_yaml_list(VHOSTS_YML, "nginx_vhosts", entry_lines):
        print(f"  Added vhost '{name}' -> {server_name} "
              f"-> http://{upstream_host_or_ip}:{port}")
        return True
    print(f"  Could not locate nginx_vhosts: block")
    return False


def cert_zones() -> list[str]:
    """Collect every DNS zone served by nginx, derived from nginx-certs.yml.

    Every `- apex: X` contributes zone X. Every `*.<zone>` extra_sans entry
    contributes zone <zone>. Bare SAN `<zone>` entries are treated as zones
    too (so e.g. scott-o-mation.com gets its own *.wildcard). Returns a
    deduped list preserving original order.
    """
    if not CERTS_YML.exists():
        return []
    try:
        import yaml
    except ImportError:
        print(f"  PyYAML not installed — can't parse nginx-certs.yml")
        return []
    with open(CERTS_YML) as f:
        cfg = yaml.safe_load(f)

    zones: list[str] = []
    seen: set[str] = set()

    def add(z: str) -> None:
        z = z.strip()
        if z and z not in seen:
            seen.add(z)
            zones.append(z)

    plays = cfg if isinstance(cfg, list) else [cfg]
    for play in plays:
        certs = ((play or {}).get("vars") or {}).get("nginx_certs") or []
        for entry in certs:
            if not isinstance(entry, dict):
                continue
            apex = entry.get("apex")
            if apex:
                add(apex)
            for san in (entry.get("extra_sans") or []):
                san = str(san).strip()
                if san.startswith("*."):
                    add(san[2:])
                else:
                    add(san)
    return zones
