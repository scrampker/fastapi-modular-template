"""Inventory edits to scottylab/ansible/inventory/workloads.yml.

Adds hosts under named groups (currently just `docker_melbourne`). Preserves
formatting — text-level indent-aware insert, not PyYAML round-trip.
"""

import re

from .paths import ANSIBLE_DIR, PROXMOX_NODE_NAME

WORKLOADS_YML = ANSIBLE_DIR / "inventory/workloads.yml"


def register(hostname: str, ansible_host_ip: str, vmid: int,
             note: str = "",
             proxmox_host: str = PROXMOX_NODE_NAME,
             group: str = "docker_melbourne") -> bool:
    """Append a host entry under <group>.hosts. Idempotent.

    `hostname` is the inventory key (e.g. `scottybiz.melbourne`). The host
    block carries `ansible_host`, `proxmox_host`, `vmid`, and an optional
    `note`.
    """
    if not WORKLOADS_YML.exists():
        print(f"  inventory/workloads.yml not found")
        return False

    text = WORKLOADS_YML.read_text()
    if re.search(rf"^\s*{re.escape(hostname)}:\s*$", text, re.M):
        print(f"  {hostname} already in inventory")
        return False

    m = re.search(rf"(^\s*{re.escape(group)}:\s*\n\s*hosts:\s*\n)",
                  text, re.M)
    if not m:
        print(f"  Could not locate {group}.hosts in inventory")
        return False

    lines = text.splitlines(keepends=True)
    start = text[: m.end(1)].count("\n")

    # Detect indent used for existing hosts (first non-blank line after)
    host_indent = "            "
    for i in range(start, len(lines)):
        ln = lines[i]
        if ln.strip() == "":
            continue
        leading = len(ln) - len(ln.lstrip(" "))
        if leading > 0:
            host_indent = " " * leading
        break

    # Find end of group's hosts block (first dedent)
    host_indent_len = len(host_indent)
    insert_at = len(lines)
    for i in range(start, len(lines)):
        ln = lines[i]
        if ln.strip() == "":
            continue
        leading = len(ln) - len(ln.lstrip(" "))
        if leading < host_indent_len:
            insert_at = i
            break
    while insert_at > start and lines[insert_at - 1].strip() == "":
        insert_at -= 1

    child = host_indent + "  "
    entry = (
        f"{host_indent}{hostname}:\n"
        f"{child}ansible_host: {ansible_host_ip}\n"
        f"{child}proxmox_host: {proxmox_host}\n"
        f"{child}vmid: {vmid}\n"
    )
    if note:
        entry += f"{child}note: \"{note}\"\n"
    lines.insert(insert_at, entry)
    WORKLOADS_YML.write_text("".join(lines))
    print(f"  Registered {hostname} -> {ansible_host_ip} in inventory")
    return True
