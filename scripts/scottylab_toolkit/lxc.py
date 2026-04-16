"""Proxmox LXC provisioning.

Creates unprivileged, Docker-capable LXCs via pct on a target Proxmox node.
Idempotent: no-op returns existing (vmid, ip) if a CT already exists with the
requested hostname.
"""

import time

from . import ssh
from .paths import (
    PCT_BRIDGE, PCT_CORES, PCT_DISK_GB, PCT_MEMORY_MB, PCT_STORAGE,
    PCT_TEMPLATE, PCT_VLAN, PROXMOX_NODE_IP,
)


def provision(hostname: str, *, cores: int = PCT_CORES,
              memory_mb: int = PCT_MEMORY_MB, disk_gb: int = PCT_DISK_GB,
              node_ip: str = PROXMOX_NODE_IP) -> tuple[int, str] | None:
    """Create an unprivileged LXC named `hostname` on the target Proxmox node.

    Returns (vmid, ipv4) on success, None on failure.

    Idempotent: if a CT with matching hostname already exists, returns its
    (vmid, ip) without any action. `pct list` columns are VMID STATUS LOCK
    NAME — the NAME column is the hostname.
    """
    find = ssh.run(
        node_ip,
        f"pct list | awk -v h={hostname} '$NF==h {{print $1}}'",
    )
    existing_vmid = find.stdout.strip().splitlines()[0] if find.stdout.strip() else ""
    if existing_vmid.isdigit():
        ip_r = ssh.run(
            node_ip,
            f"pct exec {existing_vmid} -- ip -4 -o addr show eth0 2>/dev/null "
            f"| awk '{{print $4}}' | cut -d/ -f1 | head -1",
        )
        ip = ip_r.stdout.strip()
        print(f"  LXC {hostname} already exists (CT {existing_vmid}, IP {ip})")
        return (int(existing_vmid), ip)

    create_cmd = (
        f"set -e; "
        f"VMID=$(pvesh get /cluster/nextid); "
        f"pct create $VMID {PCT_TEMPLATE} "
        f"  --hostname {hostname} "
        f"  --unprivileged 1 --features nesting=1,keyctl=1 "
        f"  --cores {cores} --memory {memory_mb} --swap 512 "
        f"  --rootfs {PCT_STORAGE}:{disk_gb} "
        f"  --net0 name=eth0,bridge={PCT_BRIDGE},ip=dhcp,tag={PCT_VLAN} "
        f"  --onboot 1 "
        f"  --ssh-public-keys /root/.ssh/authorized_keys "
        f"  --start 1 >/dev/null; "
        f"echo VMID=$VMID"
    )
    r = ssh.run(node_ip, create_cmd, timeout=180)
    if r.returncode != 0:
        print(f"  pct create failed: {r.stderr.strip()[:300]}")
        return None

    vmid_line = next((ln for ln in r.stdout.splitlines()
                      if ln.startswith("VMID=")), "")
    if not vmid_line:
        print(f"  pct create: could not parse VMID from output")
        return None
    vmid = int(vmid_line.split("=", 1)[1])

    # Poll for DHCP IP — settles within ~10s
    ip = ""
    for _ in range(30):
        time.sleep(1)
        ip_r = ssh.run(
            node_ip,
            f"pct exec {vmid} -- ip -4 -o addr show eth0 2>/dev/null "
            f"| awk '{{print $4}}' | cut -d/ -f1 | head -1",
        )
        ip = ip_r.stdout.strip()
        if ip:
            break
    if not ip:
        print(f"  CT {vmid} created but DHCP IP not seen after 30s")
        return None

    print(f"  Provisioned LXC: CT {vmid}, IP {ip}")
    return (vmid, ip)
