"""scottylab_toolkit — reusable infrastructure primitives for the Scotty homelab.

VENDORED COPY. Source of truth: /script/scottylab/automation/scottylab_toolkit/.
Vendored here 2026-04-16 to eliminate the `sys.path.insert` dependency on
/script/scottylab in scottycore-init.py. This code migrates to the new
`scottydev` repo when Shape C repo split lands (v4 Q1). Do not diverge from
upstream /script/scottylab until that split happens or until scottylab itself
is rebuilt as a scottycore app (tracked: FJ scotty/scottycore#2).

Every module here is independent of any single consumer (scottycore-init,
standalone CLIs, ansible action plugins). Pure Python stdlib where possible;
PyYAML for YAML mutation.

Modules:
  paths       — shared filesystem + network constants
  ssh         — thin wrapper around `ssh -o BatchMode=yes ...`
  yaml_inserts — indent-aware appender for ansible `vars:` list blocks
  lxc         — Proxmox LXC provisioning via pvesh + pct
  cloudflare  — CF DNS record upsert + melbourne tunnel ingress management
  unifi       — UniFi OS login + static-DNS upsert + wildcard sync
  nginx       — declarative edits to nginx-certs.yml / nginx-vhosts.yml +
                nginx-certs zone extraction
  inventory   — add hosts to scottylab/ansible/inventory/workloads.yml
  ansible_run — thin ansible-playbook runner with recap extraction

Importers:
  scottycore-init.py           (full app onboarding)
  bin/scottylab                (standalone `publish <fqdn>` etc.)
"""

from .paths import (
    SCOTTYLAB_DIR, SCOTTYLAB_WORKLOADS, ANSIBLE_DIR,
    NGINX_HOST,
    CF_API, CF_TUNNEL_ID, CF_TUNNEL_HOSTNAME, CF_TOKEN_REMOTE_PATH,
    PROXMOX_NODE_IP, PROXMOX_NODE_NAME,
    PCT_TEMPLATE, PCT_STORAGE, PCT_DISK_GB, PCT_CORES, PCT_MEMORY_MB,
    PCT_BRIDGE, PCT_VLAN,
    UNIFI_SECRETS_PATH, UNIFI_DEFAULT_AAAA,
)

__all__ = [
    "SCOTTYLAB_DIR", "SCOTTYLAB_WORKLOADS", "ANSIBLE_DIR",
    "NGINX_HOST",
    "CF_API", "CF_TUNNEL_ID", "CF_TUNNEL_HOSTNAME", "CF_TOKEN_REMOTE_PATH",
    "PROXMOX_NODE_IP", "PROXMOX_NODE_NAME",
    "PCT_TEMPLATE", "PCT_STORAGE", "PCT_DISK_GB", "PCT_CORES",
    "PCT_MEMORY_MB", "PCT_BRIDGE", "PCT_VLAN",
    "UNIFI_SECRETS_PATH", "UNIFI_DEFAULT_AAAA",
]
