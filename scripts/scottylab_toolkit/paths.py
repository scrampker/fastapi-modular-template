"""Shared constants — filesystem paths, network addresses, infra defaults."""

import os
from pathlib import Path

# Scottylab repo layout. After vendoring into scottycore/scripts/, we can no
# longer derive SCOTTYLAB_DIR from __file__ (it'd point at scottycore). The
# toolkit still operates on YAMLs that live in the scottylab repo, so the path
# is overridable via SCOTTYLAB_DIR env var with a sensible default.
# This will move to workspace-scoped config once scottydev lands (v4 Q3).
SCOTTYLAB_DIR = Path(os.environ.get("SCOTTYLAB_DIR", "/script/scottylab"))
ANSIBLE_DIR = SCOTTYLAB_DIR / "automation/ansible"
SCOTTYLAB_WORKLOADS = ANSIBLE_DIR / "playbooks/workloads"

# Networking
NGINX_HOST = "192.168.151.10"        # nginx.melbourne — reverse proxy + cloudflared

# Cloudflare + melbourne tunnel (source of truth: nginx LXC certbot token +
# hand-managed tunnel credentials). Token reused for CF DNS writes.
CF_API = "https://api.cloudflare.com/client/v4"
CF_TUNNEL_ID = "1feb72d4-9b3c-4159-a668-e552a96846c8"   # melbourne
CF_TUNNEL_HOSTNAME = f"{CF_TUNNEL_ID}.cfargotunnel.com"
CF_TOKEN_REMOTE_PATH = "/etc/letsencrypt/cloudflare.ini"

# Proxmox / LXC provisioning defaults
PROXMOX_NODE_IP = "192.168.150.101"   # proxmox1.melbourne
PROXMOX_NODE_NAME = "proxmox1"
PCT_TEMPLATE = "local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst"
PCT_STORAGE = "rbd_nvme_storage"
PCT_DISK_GB = 16
PCT_CORES = 2
PCT_MEMORY_MB = 2048
PCT_BRIDGE = "vmbr0"
PCT_VLAN = 150

# UniFi
UNIFI_SECRETS_PATH = ANSIBLE_DIR / "secrets/unifi.yml"
UNIFI_DEFAULT_AAAA = "::dead:beef"    # sentinel used for all IPv6 placeholders
