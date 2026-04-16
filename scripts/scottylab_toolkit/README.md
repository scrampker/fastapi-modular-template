# scottylab_toolkit

Reusable Python primitives for the Scotty homelab. Pulled out of
`scottycore-init.py` so anything — other tools, ad-hoc scripts, a REPL —
can orchestrate the same infrastructure operations without reimplementing
them.

## Modules

| Module | Purpose |
|---|---|
| `paths` | Shared filesystem + network constants (scottylab root, nginx host, Proxmox defaults, CF tunnel ID, UniFi secrets path) |
| `ssh` | Thin `subprocess` wrapper — `BatchMode=yes`, `ConnectTimeout=5` |
| `yaml_inserts` | Indent-aware appender for ansible `vars: <list_key>:` blocks. Preserves comments + formatting (no PyYAML round-trip) |
| `lxc` | `provision(hostname)` — idempotent unprivileged Docker-capable LXC creation via `pvesh` + `pct` |
| `cloudflare` | `ensure_cname(fqdn, apex)` + `ensure_tunnel_ingress(fqdn)` — CF DNS upsert via API and melbourne-tunnel ingress rule insertion via SSH to nginx |
| `unifi` | UniFi OS login + `ensure_dns(fqdn, ip)` (stateful with parent-wildcard coverage detection) + `sync_wildcards(zones, ip)` (declarative reconcile) |
| `nginx` | `add_cert_apex(apex)`, `add_vhost(...)` — declarative edits to the nginx playbook var lists; `cert_zones()` — parse every zone served |
| `inventory` | `register(hostname, ip, vmid, ...)` — append host under `docker_melbourne` in `inventory/workloads.yml` |
| `ansible_run` | Thin playbook runner with recap-line extraction (`install_docker`, `issue_certs`, `deploy_scottycore_app`, `publish_vhost`) |
| `cli` | `scottylab` command-line entry point (see `bin/scottylab`) |

## Idempotency

Every write function is safe to call repeatedly. State is checked before
writing, and writes are skipped (or upgraded to updates) when the
current state already matches the desired state. This is what lets
`scottycore-init.py` re-runs heal drift automatically.

## Usage from Python

```python
import sys
sys.path.insert(0, "/script/scottylab/automation")

from scottylab_toolkit import lxc, cloudflare, unifi, nginx, ansible_run
from scottylab_toolkit.paths import NGINX_HOST

# Provision
vmid, ip = lxc.provision("my-app")

# Publish
cloudflare.ensure_cname("my-app.corpaholics.com", "corpaholics.com")
cloudflare.ensure_tunnel_ingress("my-app.corpaholics.com")
unifi.ensure_dns("my-app.corpaholics.com", a_value=NGINX_HOST)

# Reconcile
unifi.sync_wildcards(nginx.cert_zones(), NGINX_HOST)
```

## CLI

See `bin/scottylab --help`. Most useful subcommand:

```
scottylab publish <fqdn> --upstream <host:port>
```

Does CF DNS + tunnel ingress + UniFi DNS + nginx vhost in a single call,
independent of any app scaffold or repo. Bring your own upstream.

## Secrets

`automation/ansible/secrets/unifi.yml` (gitignored). Template at
`unifi.yml.example`. Cloudflare DNS-01 token comes from
`/etc/letsencrypt/cloudflare.ini` on the nginx LXC (read via SSH) and is
reused for CF DNS record writes.
