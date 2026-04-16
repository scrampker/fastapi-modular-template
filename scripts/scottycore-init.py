#!/usr/bin/env python3
"""
ScottyCore Init — Onboard a new app into the ScottyCore ecosystem.
===================================================================
Wires a new app into the Phase 3 pipeline: version-pinned scottycore
dependency, server-side promote-scan + downward scottycore-upgrade
workflows, pre-commit extraction-candidate nudge hook, manager agent
with GREEN/YELLOW/RED classification responsibility.

What it does:
  1. Creates Forgejo repo (via API) + GitHub repo (via `gh`) + dual-remote push
  2. Pushes FORGEJO_TOKEN as a repo-scoped Actions secret (via API)
  3. Adds the app to config/apps.yaml
  4. Scaffolds a manager agent in .claude/agents/<app>-manager.md
  5. Injects a ScottyCore Pipeline section into the app's CLAUDE.md
  6. Installs the pre-commit /promote nudge hook
  7. Installs .forgejo/workflows/promote-scan.yml (upward pipeline)
  8. Installs .forgejo/workflows/scottycore-upgrade.yml (downward pipeline)
  9. Adds the app to scottycore's release.yml APPS fan-out list
 10. Commits + pushes app repo + scottycore-side changes

Usage:
    python3 scripts/scottycore-init.py /path/to/app                # interactive
    python3 scripts/scottycore-init.py /path/to/app --name myapp   # explicit name
    python3 scripts/scottycore-init.py /path/to/app --stack "Flask" # set stack description
    python3 scripts/scottycore-init.py /path/to/app --skip-forgejo # skip Forgejo repo creation
    python3 scripts/scottycore-init.py /path/to/app --github-repo scrampker/MyApp  # explicit GitHub repo
    python3 scripts/scottycore-init.py /path/to/new-app --scaffold   # scaffold a new app dir from scottycore
    python3 scripts/scottycore-init.py --list                       # list registered apps

If the target path does not exist, pass --scaffold (or answer 'y' at the
prompt) to create it by cloning scottycore as a template, stripping the
git history, and initializing a fresh repo with a first commit.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_DIR / "scripts"))

from config_loader import CONFIG_FILE, load_apps_config

# ── Infrastructure constants ──────────────────────────────────────────────────

FORGEJO_BASE = "https://forgejo.scotty.consulting"
FORGEJO_USER = "scotty"
FORGEJO_TOKEN_PATH = Path.home() / ".config" / "forgejo-token"

GITHUB_USER = "scrampker"

# Infra/deployment defaults
SCOTTYLAB_DIR = Path("/script/scottylab")
SCOTTYLAB_WORKLOADS = SCOTTYLAB_DIR / "automation/ansible/playbooks/workloads"
PORT_BASE = 8100  # first scottybiz-era port; scan apps.yaml for next free

# Default cert apex for new Scotty apps. Override with --domain <fqdn>.
DEFAULT_DOMAIN_APEX = "corpaholics.com"

# Cloudflare + Melbourne tunnel (used to publish apps externally)
CF_API = "https://api.cloudflare.com/client/v4"
CF_TUNNEL_ID = "1feb72d4-9b3c-4159-a668-e552a96846c8"  # melbourne
CF_TUNNEL_HOSTNAME = f"{CF_TUNNEL_ID}.cfargotunnel.com"
NGINX_HOST = "192.168.151.10"  # cloudflared + nginx reverse proxy

# The CF DNS-01 token on the nginx LXC doubles as a Zone:DNS:Edit token.
# Readable only by SSH'ing to the nginx host (root).
CF_TOKEN_REMOTE_PATH = "/etc/letsencrypt/cloudflare.ini"

# Proxmox provisioning defaults. Override via flags when adopting a different
# template, storage pool, or target node.
PROXMOX_NODE_IP = "192.168.150.101"   # proxmox1.melbourne
PROXMOX_NODE_NAME = "proxmox1"
PCT_TEMPLATE = "local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst"
PCT_STORAGE = "rbd_nvme_storage"
PCT_DISK_GB = 16
PCT_CORES = 2
PCT_MEMORY_MB = 2048
PCT_BRIDGE = "vmbr0"
PCT_VLAN = 150


def _ssh_run(host: str, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def provision_lxc(app_name: str) -> tuple[int, str] | None:
    """Create an unprivileged Docker-capable LXC on proxmox1 and wait for
    an IPv4 address. Returns (vmid, ip) on success, None on failure.

    Idempotent: if a CT with `hostname == app_name` already exists on the
    node, returns that CT's (vmid, ip) without recreating it.
    """
    # Check for existing CT with this hostname. `pct list` has columns
    # VMID STATUS LOCK NAME — the NAME column is the hostname.
    find = _ssh_run(
        PROXMOX_NODE_IP,
        f"pct list | awk -v h={app_name} '$NF==h {{print $1}}'",
    )
    existing_vmid = find.stdout.strip().splitlines()[0] if find.stdout.strip() else ""
    if existing_vmid.isdigit():
        ip_r = _ssh_run(
            PROXMOX_NODE_IP,
            f"pct exec {existing_vmid} -- ip -4 -o addr show eth0 2>/dev/null "
            f"| awk '{{print $4}}' | cut -d/ -f1 | head -1",
        )
        ip = ip_r.stdout.strip()
        print(f"  LXC for {app_name} already exists (CT {existing_vmid}, IP {ip})")
        return (int(existing_vmid), ip)

    create_cmd = (
        f"set -e; "
        f"VMID=$(pvesh get /cluster/nextid); "
        f"pct create $VMID {PCT_TEMPLATE} "
        f"  --hostname {app_name} "
        f"  --unprivileged 1 --features nesting=1,keyctl=1 "
        f"  --cores {PCT_CORES} --memory {PCT_MEMORY_MB} --swap 512 "
        f"  --rootfs {PCT_STORAGE}:{PCT_DISK_GB} "
        f"  --net0 name=eth0,bridge={PCT_BRIDGE},ip=dhcp,tag={PCT_VLAN} "
        f"  --onboot 1 "
        f"  --ssh-public-keys /root/.ssh/authorized_keys "
        f"  --start 1 >/dev/null; "
        f"echo VMID=$VMID"
    )
    r = _ssh_run(PROXMOX_NODE_IP, create_cmd, timeout=180)
    if r.returncode != 0:
        print(f"  pct create failed: {r.stderr.strip()[:300]}")
        return None

    vmid_line = next((ln for ln in r.stdout.splitlines() if ln.startswith("VMID=")), "")
    if not vmid_line:
        print(f"  pct create: could not parse VMID from output")
        return None
    vmid = int(vmid_line.split("=", 1)[1])

    # Poll for IP — DHCP usually settles within 10s
    import time
    ip = ""
    for _ in range(30):
        time.sleep(1)
        ip_r = _ssh_run(
            PROXMOX_NODE_IP,
            f"pct exec {vmid} -- ip -4 -o addr show eth0 2>/dev/null "
            f"| awk '{{print $4}}' | cut -d/ -f1 | head -1",
        )
        ip = ip_r.stdout.strip()
        if ip and ip != "":
            break
    if not ip:
        print(f"  CT {vmid} created but DHCP IP not seen after 30s")
        return None

    print(f"  Provisioned LXC: CT {vmid}, IP {ip}")
    return (vmid, ip)


def _ssh_read(host: str, path: str) -> str | None:
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", host,
         f"cat {path}"],
        capture_output=True, text=True, timeout=15,
    )
    return r.stdout if r.returncode == 0 else None


def _cf_token() -> str | None:
    """Read the Cloudflare API token from the nginx LXC's certbot config."""
    body = _ssh_read(NGINX_HOST, CF_TOKEN_REMOTE_PATH)
    if not body:
        return None
    for line in body.splitlines():
        if "dns_cloudflare_api_token" in line:
            return line.split("=", 1)[1].strip()
    return None


def _cf_zone_id(token: str, apex: str) -> str | None:
    req = urllib.request.Request(
        f"{CF_API}/zones?name={apex}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            results = data.get("result", [])
            return results[0]["id"] if results else None
    except Exception as e:
        print(f"  CF zone lookup failed: {e}")
        return None


def ensure_cf_cname(fqdn: str, apex: str) -> bool:
    """Create a CNAME <fqdn> -> <tunnel>.cfargotunnel.com on Cloudflare
    (proxied). Idempotent: no-op if an identical record already exists.
    """
    token = _cf_token()
    if not token:
        print(f"  No Cloudflare token available — skipping DNS")
        return False
    zone_id = _cf_zone_id(token, apex)
    if not zone_id:
        print(f"  Cloudflare zone '{apex}' not found — skipping DNS")
        return False

    name = fqdn.removesuffix(f".{apex}") if fqdn != apex else "@"
    # Probe existing record
    probe = urllib.request.Request(
        f"{CF_API}/zones/{zone_id}/dns_records?type=CNAME&name={fqdn}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(probe, timeout=15) as resp:
            existing = json.loads(resp.read()).get("result", [])
    except Exception as e:
        print(f"  CF DNS probe failed: {e}")
        return False

    if existing and existing[0].get("content") == CF_TUNNEL_HOSTNAME:
        print(f"  CF CNAME {fqdn} already points to tunnel")
        return True

    payload = json.dumps({
        "type": "CNAME",
        "name": name,
        "content": CF_TUNNEL_HOSTNAME,
        "proxied": True,
        "comment": "scottycore app — managed by scottycore-init.py",
    }).encode()
    method = "PUT" if existing else "POST"
    url = (f"{CF_API}/zones/{zone_id}/dns_records/{existing[0]['id']}"
           if existing else
           f"{CF_API}/zones/{zone_id}/dns_records")
    req = urllib.request.Request(
        url, data=payload, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        if data.get("success"):
            print(f"  CF CNAME {fqdn} -> {CF_TUNNEL_HOSTNAME} ({method})")
            return True
        print(f"  CF DNS error: {data.get('errors')}")
        return False
    except urllib.error.HTTPError as e:
        print(f"  CF DNS HTTP {e.code}: {e.read().decode()[:200]}")
        return False


def ensure_tunnel_ingress(fqdn: str) -> bool:
    """Add a tunnel ingress rule for <fqdn> -> nginx on the melbourne
    cloudflared config. SSH-edits /etc/cloudflared/config.yml on nginx
    host; idempotent — no-op if the hostname rule is already present.
    Restarts cloudflared after a change.
    """
    # Idempotency probe
    check = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", NGINX_HOST,
         f"grep -q 'hostname: {fqdn}' /etc/cloudflared/config.yml && echo present"],
        capture_output=True, text=True, timeout=15,
    )
    if "present" in check.stdout:
        print(f"  tunnel ingress for {fqdn} already present")
        return True

    # Insert the new rule before the terminal `- service: http_status:404` line.
    # Using a heredoc on the remote side keeps the script tiny and avoids
    # quoting foot-guns around YAML whitespace.
    remote_cmd = f"""
set -e
CONF=/etc/cloudflared/config.yml
cp $CONF $CONF.bak.$(date +%s)
python3 - <<'PY'
import re
conf = open('/etc/cloudflared/config.yml').read()
rule = '''  - hostname: {fqdn}
    service: http://localhost:80
    originRequest:
      httpHostHeader: {fqdn}
'''
# Insert before the terminal catch-all 404 rule
pattern = r'(\\n)(\\s*- service:\\s*http_status:404\\s*\\n)'
new = re.sub(pattern, r'\\1' + rule + r'\\2', conf, count=1)
if new == conf:
    raise SystemExit('catch-all rule not found — cannot insert')
open('/etc/cloudflared/config.yml', 'w').write(new)
PY
cloudflared tunnel --config $CONF ingress validate >/dev/null
systemctl restart cloudflared
"""
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", NGINX_HOST, remote_cmd],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        print(f"  tunnel ingress update failed: {r.stderr.strip()[:300]}")
        return False
    print(f"  tunnel ingress for {fqdn} added; cloudflared restarted")
    return True


# ── Step 1: Forgejo repo + dual-remote ───────────────────────────────────────


def _read_forgejo_token() -> str | None:
    if FORGEJO_TOKEN_PATH.exists():
        return FORGEJO_TOKEN_PATH.read_text().strip()
    return None


def create_forgejo_repo(app_name: str, description: str, default_branch: str) -> str | None:
    """Create a repo on Forgejo. Returns the clone URL or None on failure."""
    token = _read_forgejo_token()
    if not token:
        print(f"  No Forgejo token at {FORGEJO_TOKEN_PATH} — skipping repo creation")
        return None

    payload = json.dumps({
        "name": app_name,
        "description": description,
        "private": False,
        "default_branch": default_branch,
    }).encode()

    req = urllib.request.Request(
        f"{FORGEJO_BASE}/api/v1/user/repos",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"token {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            url = data.get("clone_url", "")
            print(f"  Created Forgejo repo: {data.get('html_url', url)}")
            return url
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if "already exists" in body.lower():
            url = f"{FORGEJO_BASE}/{FORGEJO_USER}/{app_name}.git"
            print(f"  Forgejo repo already exists: {url}")
            return url
        print(f"  Forgejo API error ({e.code}): {body[:200]}")
        return None
    except Exception as e:
        print(f"  Forgejo connection error: {e}")
        return None


def set_forgejo_secret(app_name: str, secret_name: str, secret_value: str) -> bool:
    """Set a repo-scoped Actions secret on the new Forgejo repo.

    Uses PUT /api/v1/repos/{owner}/{repo}/actions/secrets/{name} which
    upserts. Required so the promote-scan + scottycore-upgrade workflows
    can call back into Forgejo (create PRs, dispatch workflows, comment).
    """
    token = _read_forgejo_token()
    if not token:
        print(f"  No Forgejo token — cannot set {secret_name} secret")
        return False

    payload = json.dumps({"data": secret_value}).encode()
    req = urllib.request.Request(
        f"{FORGEJO_BASE}/api/v1/repos/{FORGEJO_USER}/{app_name}/actions/secrets/{secret_name}",
        data=payload,
        method="PUT",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"token {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            code = resp.status
        if code in (201, 204):
            print(f"  Set repo secret {secret_name} on Forgejo")
            return True
        print(f"  Unexpected response setting {secret_name}: HTTP {code}")
        return False
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  Forgejo secrets API error ({e.code}): {body[:200]}")
        return False
    except Exception as e:
        print(f"  Forgejo secrets connection error: {e}")
        return False


def create_github_repo(app_name: str, description: str, private: bool = True) -> bool:
    """Create a GitHub repo via `gh repo create`. Idempotent: treats
    'already exists' as success. Requires `gh` authenticated.
    """
    probe = subprocess.run(
        ["gh", "repo", "view", f"{GITHUB_USER}/{app_name}"],
        capture_output=True, text=True, timeout=15,
    )
    if probe.returncode == 0:
        print(f"  GitHub repo already exists: github.com/{GITHUB_USER}/{app_name}")
        return True

    visibility = "--private" if private else "--public"
    result = subprocess.run(
        ["gh", "repo", "create", f"{GITHUB_USER}/{app_name}",
         visibility, "--description", description],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        print(f"  Created GitHub repo: github.com/{GITHUB_USER}/{app_name}")
        return True
    err = result.stderr.strip()
    if "already exists" in err.lower():
        print(f"  GitHub repo already exists: github.com/{GITHUB_USER}/{app_name}")
        return True
    print(f"  gh repo create failed: {err[:200]}")
    return False


def setup_dual_remote(app_path: Path, forgejo_url: str, github_url: str, branch: str) -> bool:
    """Configure dual-remote: origin fetches from Forgejo, pushes to both."""

    def git(*args: str) -> bool:
        r = subprocess.run(["git", "-C", str(app_path)] + list(args),
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0

    # Check current state
    result = subprocess.run(
        ["git", "-C", str(app_path), "remote", "-v"],
        capture_output=True, text=True, timeout=10,
    )
    remotes = result.stdout

    if forgejo_url in remotes:
        print(f"  Dual-remote already configured")
        return True

    # Ensure origin exists (add if missing, set-url if already configured)
    if "origin" in remotes:
        git("remote", "set-url", "origin", forgejo_url)
    else:
        git("remote", "add", "origin", forgejo_url)
    # Set origin push to both Forgejo and GitHub
    git("remote", "set-url", "--push", "origin", forgejo_url)
    git("remote", "set-url", "--add", "--push", "origin", github_url)
    # Add github as a separate remote for convenience (no-op if already present)
    if "github" not in remotes:
        git("remote", "add", "github", github_url)

    print(f"  Configured dual-remote:")
    print(f"    origin fetch: {forgejo_url}")
    print(f"    origin push:  {forgejo_url} + {github_url}")

    return True


def detect_github_remote(app_path: Path) -> str | None:
    """Try to find an existing GitHub remote URL."""
    result = subprocess.run(
        ["git", "-C", str(app_path), "remote", "-v"],
        capture_output=True, text=True, timeout=10,
    )
    for line in result.stdout.splitlines():
        if "github.com" in line and "(push)" in line:
            parts = line.split()
            if len(parts) >= 2:
                return parts[1]
    return None


# ── Step 2: Register in config/apps.yaml ─────────────────────────────────────


def pick_next_port(existing: dict) -> int:
    """Pick the next free port starting from PORT_BASE, skipping any port
    already recorded on a registered app. Dev apps on 192.168.150.6 and
    containerized apps share the same port namespace so they stay unique.
    """
    used = {
        int(cfg["port"])
        for cfg in existing.values()
        if isinstance(cfg, dict) and str(cfg.get("port", "")).isdigit()
    }
    port = PORT_BASE
    while port in used:
        port += 1
    return port


def register_app(app_path: Path, app_name: str, stack: str, branch: str,
                 port: int, fqdn: str) -> bool:
    """Add app to config/apps.yaml. Returns True if added, False if already present."""
    existing = load_apps_config()
    if app_name in existing:
        print(f"  '{app_name}' is already registered in config/apps.yaml")
        return False

    entry = f"""
{app_name}:
  path: {app_path}
  stack: {stack}
  branch: {branch}
  port: {port}
  fqdn: {fqdn}
"""
    with open(CONFIG_FILE, "a") as f:
        f.write(entry)

    print(f"  Added '{app_name}' to config/apps.yaml (port={port}, fqdn={fqdn})")
    return True


# ── docker-compose.yml in the scaffolded app ────────────────────────────────


def install_docker_compose(app_path: Path, app_name: str, port: int) -> bool:
    """Render docker-compose.yml.template into the new app with the assigned port."""
    src = CORE_DIR / "docker-compose.yml.template"
    dest = app_path / "docker-compose.yml"
    if not src.exists():
        print(f"  docker-compose template missing at {src}")
        return False
    if dest.exists():
        print(f"  docker-compose.yml already present in {app_name}")
        return False
    rendered = (src.read_text()
                .replace("__APP_NAME__", app_name)
                .replace("__APP_PORT__", str(port)))
    dest.write_text(rendered)
    print(f"  Rendered docker-compose.yml (port {port}) into {app_name}")
    return True


# ── scottylab infra declarations (nginx cert + vhost + scottycore-apps) ─────


def _scottylab_available() -> bool:
    if not SCOTTYLAB_WORKLOADS.exists():
        print(f"  scottylab not found at {SCOTTYLAB_DIR} — skipping infra edits")
        return False
    return True


def _append_to_yaml_list(path: Path, list_key: str, entry_lines: list[str]) -> bool:
    """Append entry_lines to a YAML list named `list_key` in the given file.

    Locates the `<indent><list_key>:` line, then walks forward accepting only
    lines whose indent > list_key indent (i.e. list body), stopping at the
    first line that dedents back to <= list_key indent. Inserts the new
    entry immediately before that exit point.

    entry_lines should be written at the same indent as existing list items
    (typically list_key_indent + 2). The caller supplies properly indented
    strings without a trailing newline — this helper joins them.
    """
    lines = path.read_text().splitlines(keepends=True)
    key_idx = None
    key_indent = 0
    for i, ln in enumerate(lines):
        m = re.match(rf"^(\s*){re.escape(list_key)}:\s*$", ln)
        if m:
            key_idx = i
            key_indent = len(m.group(1))
            break
    if key_idx is None:
        # Handle inline empty list form: `<indent><list_key>: []`
        for i, ln in enumerate(lines):
            m = re.match(rf"^(\s*){re.escape(list_key)}:\s*\[\]\s*$", ln)
            if m:
                indent = m.group(1)
                replacement = f"{indent}{list_key}:\n"
                for el in entry_lines:
                    replacement += el if el.endswith("\n") else el + "\n"
                lines[i] = replacement
                path.write_text("".join(lines))
                return True
        return False

    # Scan forward through the list body
    exit_idx = len(lines)
    for j in range(key_idx + 1, len(lines)):
        ln = lines[j]
        if ln.strip() == "":
            continue
        leading = len(ln) - len(ln.lstrip(" "))
        if leading <= key_indent:
            exit_idx = j
            break

    # Insert entry just before exit (and before any trailing blank line that
    # separates the vars block from the next section, to keep formatting).
    insert_at = exit_idx
    while insert_at > key_idx + 1 and lines[insert_at - 1].strip() == "":
        insert_at -= 1

    snippet = "".join(el if el.endswith("\n") else el + "\n" for el in entry_lines)
    lines.insert(insert_at, snippet)
    path.write_text("".join(lines))
    return True


def add_nginx_cert_apex(apex: str) -> bool:
    """Append a new apex to nginx-certs.yml if not already present.
    Declarative only — no cert is issued until ansible-playbook runs.
    """
    if not _scottylab_available():
        return False
    certs_yml = SCOTTYLAB_WORKLOADS / "nginx-certs.yml"
    if not certs_yml.exists():
        print(f"  nginx-certs.yml not found")
        return False

    text = certs_yml.read_text()
    if re.search(rf"^\s*-\s*apex:\s*{re.escape(apex)}\s*$", text, re.M):
        print(f"  {apex} already in nginx-certs.yml")
        return False

    if _append_to_yaml_list(certs_yml, "nginx_certs", [f"      - apex: {apex}"]):
        print(f"  Added apex '{apex}' to nginx-certs.yml")
        return True
    print(f"  Could not locate nginx_certs: block")
    return False


def add_nginx_vhost(app_name: str, fqdn: str, apex: str,
                    upstream_host_or_ip: str, port: int) -> bool:
    """Append a vhost entry to nginx-vhosts.yml."""
    if not _scottylab_available():
        return False
    vhosts_yml = SCOTTYLAB_WORKLOADS / "nginx-vhosts.yml"
    if not vhosts_yml.exists():
        print(f"  nginx-vhosts.yml not found")
        return False

    text = vhosts_yml.read_text()
    if re.search(rf"^\s*-\s*name:\s*{re.escape(app_name)}\s*$", text, re.M):
        print(f"  vhost '{app_name}' already in nginx-vhosts.yml")
        return False

    entry_lines = [
        "",
        f"      - name: {app_name}",
        f"        server_name: {fqdn}",
        f"        cert: {apex}",
        f"        upstream: http://{upstream_host_or_ip}:{port}",
    ]
    if _append_to_yaml_list(vhosts_yml, "nginx_vhosts", entry_lines):
        print(f"  Added vhost '{app_name}' -> {fqdn} -> http://{upstream_host_or_ip}:{port}")
        return True
    print(f"  Could not locate nginx_vhosts: block")
    return False


def add_scottycore_app(app_name: str, target_host: str, port: int,
                       repo_url: str, branch: str) -> bool:
    """Append an entry to scottycore-apps.yml."""
    if not _scottylab_available():
        return False
    apps_yml = SCOTTYLAB_WORKLOADS / "scottycore-apps.yml"
    if not apps_yml.exists():
        print(f"  scottycore-apps.yml not found (expected template to be checked in)")
        return False

    text = apps_yml.read_text()
    if re.search(rf"^\s*-\s*name:\s*{re.escape(app_name)}\s*$", text, re.M):
        print(f"  scottycore-apps entry '{app_name}' already present")
        return False

    entry_lines = [
        f"      - name: {app_name}",
        f"        host: {target_host}",
        f"        port: {port}",
        f"        repo: {repo_url}",
        f"        branch: {branch}",
    ]
    if not _append_to_yaml_list(apps_yml, "scottycore_apps", entry_lines):
        print(f"  Could not locate scottycore_apps: block")
        return False
    print(f"  Added scottycore-apps entry '{app_name}' -> {target_host}")
    return True


# ── UniFi DNS (gateway-local DNS) ──────────────────────────────────────────

UNIFI_SECRETS_PATH = SCOTTYLAB_DIR / "automation/ansible/secrets/unifi.yml"
UNIFI_STATIC_DNS_PATH = "/proxy/network/v2/api/site/{site}/static-dns"
UNIFI_DEFAULT_AAAA = "::dead:beef"  # sentinel used for all IPv6 placeholders


def _load_unifi_creds() -> dict | None:
    """Read and validate secrets/unifi.yml. Returns None if missing or
    still carrying the placeholder password — lets the caller skip
    gracefully rather than fail the whole onboarding.
    """
    if not UNIFI_SECRETS_PATH.exists():
        return None
    try:
        import yaml  # lazy import — avoid hard dep if caller skips unifi
    except ImportError:
        print(f"  PyYAML not installed — can't read unifi.yml")
        return None
    try:
        with open(UNIFI_SECRETS_PATH) as f:
            cfg = yaml.safe_load(f)
    except Exception as e:
        print(f"  failed to parse unifi.yml: {e}")
        return None
    u = (cfg or {}).get("unifi") or {}
    if not u.get("password") or "<" in str(u.get("password")):
        print(f"  unifi.yml password still has a placeholder — skipping UniFi DNS")
        return None
    return u


def _unifi_login(u: dict) -> tuple[dict, dict] | None:
    """Log in to UniFi OS. Returns (cookies_dict, headers_dict) or None."""
    import http.cookiejar
    import ssl
    ctx = ssl.create_default_context()
    if not u.get("verify_ssl", False):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(jar),
    )
    payload = json.dumps({
        "username": u["username"],
        "password": u["password"],
    }).encode()
    req = urllib.request.Request(
        f"{u['base_url']}/api/auth/login",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    csrf = ""
    try:
        with opener.open(req, timeout=15) as resp:
            if resp.status != 200:
                print(f"  UniFi login HTTP {resp.status}")
                return None
            # UniFi OS returns the CSRF token we must send on write calls
            # via the `X-CSRF-Token` (or `X-Updated-CSRF-Token`) response
            # header. The TOKEN cookie is JWT-only and not directly usable.
            csrf = (resp.headers.get("X-CSRF-Token")
                    or resp.headers.get("X-Updated-CSRF-Token")
                    or "")
    except Exception as e:
        print(f"  UniFi login failed: {e}")
        return None

    cookies = {c.name: c.value for c in jar}
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    return (cookies, headers), opener  # type: ignore[return-value]


def _unifi_request(u: dict, opener, method: str, path: str,
                   headers: dict, body: dict | None = None) -> dict | list | None:
    url = f"{u['base_url']}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json",
        **headers,
    })
    try:
        with opener.open(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        print(f"  UniFi {method} {path} -> HTTP {e.code}: {e.read().decode()[:200]}")
        return None
    except Exception as e:
        print(f"  UniFi {method} {path} failed: {e}")
        return None


def _wildcard_covers(records: list, fqdn: str, rec_type: str, value: str) -> bool:
    """True iff a pre-existing `*.<parent>` record with the same type+value
    already resolves `fqdn`. We match any proper-parent wildcard: for
    `a.b.c.d.com`, we check `*.b.c.d.com`, `*.c.d.com`, `*.d.com`.
    """
    parts = fqdn.split(".")
    for i in range(1, len(parts) - 1):  # leave at least one dot in the parent
        wildcard = "*." + ".".join(parts[i:])
        for r in records:
            if (r.get("key") == wildcard
                    and r.get("record_type") == rec_type
                    and r.get("value") == value
                    and r.get("enabled", True)):
                return True
    return False


def ensure_unifi_dns(fqdn: str, a_value: str,
                     aaaa_value: str = UNIFI_DEFAULT_AAAA) -> bool:
    """Upsert A + AAAA records for `fqdn` on the local UniFi gateway.

    Stateful: for each record type, fetch existing records first and
      - no-op if a parent wildcard with the same value already covers fqdn
        (matches the existing `*.<apex>` pattern)
      - no-op if a specific record with the right value already exists
      - update if a specific record exists with a wrong value
      - create otherwise
    """
    u = _load_unifi_creds()
    if u is None:
        return False

    auth = _unifi_login(u)
    if auth is None:
        return False
    (_cookies, headers), opener = auth
    site = u.get("site", "default")
    path = UNIFI_STATIC_DNS_PATH.format(site=site)

    records = _unifi_request(u, opener, "GET", path, headers)
    if records is None:
        return False

    def upsert(rec_type: str, value: str) -> None:
        if _wildcard_covers(records, fqdn, rec_type, value):
            print(f"  UniFi: {fqdn} {rec_type} covered by existing wildcard ({value})")
            return
        existing = [r for r in records
                    if r.get("key") == fqdn and r.get("record_type") == rec_type]
        if existing:
            cur = existing[0]
            if cur.get("value") == value and cur.get("enabled", True):
                print(f"  UniFi: {fqdn} {rec_type} already = {value}")
                return
            body = {**cur, "value": value, "enabled": True}
            r = _unifi_request(u, opener, "PUT", f"{path}/{cur['_id']}",
                               headers, body)
            if r is not None:
                print(f"  UniFi: updated {fqdn} {rec_type} = {value}")
            return
        body = {
            "key": fqdn,
            "record_type": rec_type,
            "value": value,
            "enabled": True,
            "ttl": 0,
            "port": 0,
            "priority": 0,
            "weight": 0,
        }
        r = _unifi_request(u, opener, "POST", path, headers, body)
        if r is not None:
            print(f"  UniFi: created {fqdn} {rec_type} = {value}")

    upsert("A", a_value)
    upsert("AAAA", aaaa_value)
    return True


def _nginx_cert_zones() -> list[str]:
    """Collect every DNS zone served by nginx, as derived from nginx-certs.yml.

    Source of truth: each `- apex: X` gives zone X. Each `*.<zone>` in
    `extra_sans` gives zone <zone>. A bare `<zone>` SAN is treated as
    a zone too (so e.g. scott-o-mation.com gets its own *.wildcard).
    De-duplicated, order preserved.
    """
    if not _scottylab_available():
        return []
    certs_yml = SCOTTYLAB_WORKLOADS / "nginx-certs.yml"
    if not certs_yml.exists():
        return []
    try:
        import yaml
    except ImportError:
        print(f"  PyYAML not installed — can't read nginx-certs.yml")
        return []
    with open(certs_yml) as f:
        cfg = yaml.safe_load(f)

    zones: list[str] = []
    seen: set[str] = set()

    def add(z: str) -> None:
        z = z.strip()
        if z and z not in seen:
            seen.add(z)
            zones.append(z)

    # The file is a playbook: cfg is a list of plays; find the play with vars
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


def sync_unifi_wildcards() -> bool:
    """Ensure every zone served by nginx has `*.<zone>` A+AAAA records in
    UniFi. Purely declarative from `nginx-certs.yml` — rerun-safe, and
    heals anything deleted out-of-band.
    """
    u = _load_unifi_creds()
    if u is None:
        return False
    zones = _nginx_cert_zones()
    if not zones:
        print(f"  no zones found in nginx-certs.yml — nothing to sync")
        return False

    auth = _unifi_login(u)
    if auth is None:
        return False
    (_cookies, headers), opener = auth
    site = u.get("site", "default")
    path = UNIFI_STATIC_DNS_PATH.format(site=site)

    records = _unifi_request(u, opener, "GET", path, headers)
    if records is None:
        return False

    created = updated = unchanged = 0

    def upsert_wildcard(zone: str, rec_type: str, value: str) -> None:
        nonlocal created, updated, unchanged
        key = f"*.{zone}"
        existing = [r for r in records
                    if r.get("key") == key and r.get("record_type") == rec_type]
        if existing:
            cur = existing[0]
            if cur.get("value") == value and cur.get("enabled", True):
                unchanged += 1
                return
            body = {**cur, "value": value, "enabled": True}
            r = _unifi_request(u, opener, "PUT", f"{path}/{cur['_id']}",
                               headers, body)
            if r is not None:
                print(f"  UniFi: updated {key} {rec_type} = {value}")
                updated += 1
            return
        body = {
            "key": key,
            "record_type": rec_type,
            "value": value,
            "enabled": True,
            "ttl": 0, "port": 0, "priority": 0, "weight": 0,
        }
        r = _unifi_request(u, opener, "POST", path, headers, body)
        if r is not None:
            print(f"  UniFi: created {key} {rec_type} = {value}")
            created += 1

    for zone in zones:
        upsert_wildcard(zone, "A", NGINX_HOST)
        upsert_wildcard(zone, "AAAA", UNIFI_DEFAULT_AAAA)

    print(f"  UniFi wildcard sync: {len(zones)} zones, "
          f"{created} created, {updated} updated, {unchanged} unchanged")
    return True


def register_in_inventory(app_name: str, target_host: str, ip: str,
                          vmid: int, note: str) -> bool:
    """Append <target_host> entry under docker_melbourne in workloads.yml."""
    if not _scottylab_available():
        return False
    inv = SCOTTYLAB_DIR / "automation/ansible/inventory/workloads.yml"
    if not inv.exists():
        print(f"  inventory/workloads.yml not found")
        return False

    text = inv.read_text()
    if re.search(rf"^\s*{re.escape(target_host)}:\s*$", text, re.M):
        print(f"  {target_host} already in inventory")
        return False

    # Find docker_melbourne: and its hosts: block, append a new host there.
    m = re.search(r"(^\s*docker_melbourne:\s*\n\s*hosts:\s*\n)", text, re.M)
    if not m:
        print(f"  Could not locate docker_melbourne.hosts in inventory")
        return False

    lines = text.splitlines(keepends=True)
    # Index of the line right after `hosts:` under docker_melbourne
    start = text[: m.end(1)].count("\n")
    # Determine the indent used for existing hosts (first non-blank line after)
    host_indent = "            "
    for i in range(start, len(lines)):
        ln = lines[i]
        if ln.strip() == "":
            continue
        leading = len(ln) - len(ln.lstrip(" "))
        # existing hosts sit at a deeper indent than `hosts:`
        if leading > 0:
            host_indent = " " * leading
        break

    # Walk forward to the end of docker_melbourne.hosts block (first dedent)
    insert_at = len(lines)
    host_indent_len = len(host_indent)
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
        f"{host_indent}{target_host}:\n"
        f"{child}ansible_host: {ip}\n"
        f"{child}proxmox_host: {PROXMOX_NODE_NAME}\n"
        f"{child}vmid: {vmid}\n"
        f"{child}note: \"{note}\"\n"
    )
    lines.insert(insert_at, entry)
    inv.write_text("".join(lines))
    print(f"  Registered {target_host} -> {ip} in inventory")
    return True


# ── Ansible runners (end-to-end: install docker, deploy, publish) ───────────


ANSIBLE_DIR = SCOTTYLAB_DIR / "automation/ansible"


def _ansible_run(playbook: str, *, limit: str | None = None,
                 extra_vars: dict | None = None) -> bool:
    cmd = [
        "ansible-playbook",
        "-i", "inventory/hosts.yml",
        "-i", "inventory/workloads.yml",
        f"playbooks/workloads/{playbook}",
    ]
    if limit:
        cmd += ["-l", limit]
    if extra_vars:
        for k, v in extra_vars.items():
            cmd += ["-e", f"{k}={v}"]
    r = subprocess.run(cmd, cwd=str(ANSIBLE_DIR),
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        tail = (r.stdout + r.stderr).splitlines()[-20:]
        print(f"  ansible {playbook} FAILED:")
        for ln in tail:
            print(f"    {ln}")
        return False
    # Parse recap line for brevity
    recap = [ln for ln in r.stdout.splitlines()
             if re.search(r"\s:\s+ok=\d+", ln)]
    for ln in recap:
        print(f"  {ln.strip()}")
    return True


def install_docker_on(host: str) -> bool:
    print(f"  installing Docker on {host}...")
    return _ansible_run("docker.yml", limit=host, extra_vars={"target_hosts": "all"})


def issue_certs() -> bool:
    print(f"  issuing/expanding Let's Encrypt certs...")
    return _ansible_run("nginx-certs.yml")


def deploy_scottycore_app(host: str) -> bool:
    print(f"  deploying scottycore app on {host}...")
    return _ansible_run("scottycore-apps.yml", limit=host,
                        extra_vars={"target_hosts": "all"})


def publish_vhost() -> bool:
    print(f"  publishing nginx vhost...")
    return _ansible_run("nginx-vhosts.yml")


def commit_scottylab_changes(app_name: str) -> bool:
    """Commit + push scottylab infra declarations."""
    if not _scottylab_available():
        return False

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(SCOTTYLAB_DIR)] + list(args),
            capture_output=True, text=True, timeout=30,
        )

    git("add",
        "automation/ansible/playbooks/workloads/nginx-certs.yml",
        "automation/ansible/playbooks/workloads/nginx-vhosts.yml",
        "automation/ansible/playbooks/workloads/scottycore-apps.yml")

    status = git("diff", "--cached", "--stat")
    if not status.stdout.strip():
        print(f"  No scottylab changes to commit")
        return False

    r = git("commit", "-m", f"feat: wire {app_name} into nginx + scottycore-apps")
    if r.returncode != 0:
        print(f"  scottylab commit failed: {r.stderr.strip()[:200]}")
        return False
    push = git("push")
    if push.returncode == 0:
        print(f"  Committed and pushed scottylab changes")
    else:
        print(f"  Committed locally but push failed: {push.stderr.strip()[:200]}")
    return True


# ── Step 3: Scaffold manager agent ──────────────────────────────────────────


def scaffold_agent(app_name: str, app_path: Path, stack: str) -> bool:
    """Create .claude/agents/<app>-manager.md in scottycore."""
    agents_dir = CORE_DIR / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agents_dir / f"{app_name}-manager.md"

    if agent_file.exists():
        print(f"  Agent already exists: {agent_file.name}")
        return False

    content = f"""# {app_name} Manager Agent

You are the dedicated manager agent for **{app_name}** (`{app_path}`).
Stack: {stack}.

## Pipeline state

| Direction | Workflow | Status |
|---|---|---|
| Upward (contribute to scottycore) | `.forgejo/workflows/promote-scan.yml` | Installed |
| Downward (receive scottycore bumps) | `.forgejo/workflows/scottycore-upgrade.yml` | Installed if app has root `pyproject.toml`, else deferred |

## Your Responsibilities

### 1. Autonomous scottycore upgrade-PR review

When invoked from `.forgejo/workflows/scottycore-upgrade.yml` on a bump PR:
classify GREEN / YELLOW / RED based on CI + breaking-change impact. Emit a
single JSON object:
```json
{{"classification":"GREEN|YELLOW|RED","comment":"...","bump":"patch|minor|major","follow_up_issue":""}}
```

Bias rules:
- Prefer GREEN when genuinely uneventful
- Prefer RED over YELLOW when unsure (false RED costs 2 min; false GREEN may ship break)
- Never fabricate breaking-change impact — if grep finds no hits, the change doesn't touch this app

### 2. Interactive /promote review

When the user invokes `/promote <path>` or asks whether code belongs in scottycore,
analyze the target and output the same JSON schema. Be strict — prefer RED on any
app-specific concept leakage.

### 3. Feature implementation

- Check scottycore first (`/script/scottycore/scottycore/`) for modules covering your need
- Prefer consuming scottycore over re-implementing
- Stack-specific concerns stay local

### 4. Session-start hygiene

On session open:
1. `git fetch --quiet origin`
2. Query Forgejo for scottycore-bot comments on the last 5 commits
3. Report any pipeline activity the user missed

See the app's `CLAUDE.md` for the concrete snippet.

## Key directories
- App root: `{app_path}`
- ScottyCore: `/script/scottycore`

## What you do NOT do

- Port fixes by hand from other Scotty apps (old core-sync model is retired)
- Reference `.scottycore-patterns.yaml` (retired in Phase 4)
- Edit scottycore directly — use `/promote` or push a commit the server-side classifier will pick up
- Block commits on the pre-commit nudge (advisory only)
"""

    agent_file.write_text(content)
    print(f"  Created agent: {agent_file.name}")
    return True


# ── Step 5: Inject ScottyCore section into app CLAUDE.md ────────────────────

CLAUDE_SECTION_MARKER = "<!-- scottycore-integration -->"

CLAUDE_TEMPLATE = """
<!-- scottycore-integration -->
## ScottyCore Pipeline

This app is part of the **Scotty app family** and depends on the
[scottycore](/script/scottycore) package (pinned in `pyproject.toml` via
`git+https://forgejo.scotty.consulting/scotty/scottycore.git@vX.Y.Z`).

### Automated pipeline

**Upward — `.forgejo/workflows/promote-scan.yml`**: every push to master runs
a `claude -p` classifier on the Python diff. If code looks like shared-library
material, it dispatches scottycore's `promote-receive.yml` for autonomous
extraction + release.

**Downward — `.forgejo/workflows/scottycore-upgrade.yml`** (if installed):
when scottycore cuts a release, this app receives a workflow_dispatch that
bumps the pin, runs CI, and has the manager agent classify the PR as
GREEN / YELLOW / RED.

### `/promote` skill

To proactively move code to scottycore without waiting for the next push,
invoke `/promote <path>` from a Claude Code session.

### Kill switches

- `export SCOTTYCORE_SKIP_PROMOTE_NUDGE=1` — disables local pre-commit nudge
- Remove `.forgejo/workflows/promote-scan.yml` — disables server-side scan
<!-- /scottycore-integration -->

## Session-start: check pipeline feedback

```bash
git fetch --quiet origin
TOKEN=$(cat ~/.config/forgejo-token 2>/dev/null)
[ -n "$TOKEN" ] && for sha in $(git log -5 --format='%H' origin/master); do
    COMMENTS=$(curl -s -H "Authorization: token $TOKEN" \\
      "https://forgejo.scotty.consulting/api/v1/repos/scotty/APP_NAME/commits/$sha/comments")
    if echo "$COMMENTS" | grep -q '"body"'; then
        echo "=== $sha ==="
        echo "$COMMENTS" | python3 -c "import json,sys;[print(c['body']) for c in json.load(sys.stdin)]"
    fi
done
```
Report any scottycore-bot comments before starting work.
"""


def inject_claude_section(app_path: Path) -> bool:
    """Add a ScottyCore section to the app's CLAUDE.md."""
    claude_md = app_path / "CLAUDE.md"

    if not claude_md.exists():
        print(f"  No CLAUDE.md found — creating one with ScottyCore section")
        claude_md.write_text(f"# {app_path.name}\n{CLAUDE_TEMPLATE.replace('{app_path}', str(app_path))}")
        return True

    content = claude_md.read_text()

    if CLAUDE_SECTION_MARKER in content:
        print(f"  ScottyCore section already present in CLAUDE.md")
        return False

    section = CLAUDE_TEMPLATE.replace("{app_path}", str(app_path))
    content = content.rstrip() + "\n" + section
    claude_md.write_text(content)
    print(f"  Injected ScottyCore section into CLAUDE.md")
    return True


# ── Step 6: Commit + push ───────────────────────────────────────────────────


def install_promote_scan_workflow(app_path: Path, app_name: str) -> bool:
    """Install the server-side promote-scan.yml workflow in the app repo.

    Runs on every push to master, classifies the diff for scottycore-worthy
    extraction candidates, and dispatches scottycore's promote-receive.yml
    when appropriate. The authoritative path for upward sync — the local
    pre-commit nudge is only an early warning.
    """
    src = CORE_DIR / ".claude" / "templates" / "promote-scan.yml"
    workflows_dir = app_path / ".forgejo" / "workflows"
    dest = workflows_dir / "promote-scan.yml"

    if not src.exists():
        print(f"  template missing at {src}")
        return False
    workflows_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text())
    print(f"  installed .forgejo/workflows/promote-scan.yml in {app_name}")
    return True


def install_scottycore_upgrade_workflow(app_path: Path, app_name: str, branch: str) -> bool:
    """Install .forgejo/workflows/scottycore-upgrade.yml in the app repo.

    This is the downward half of the pipeline — receives workflow_dispatch
    from scottycore's release.yml on a tagged release, bumps the pin, opens
    a PR, and has the manager agent classify it GREEN/YELLOW/RED.

    Template placeholders: __APP_NAME__, __BRANCH__.
    Only installed if the app has a root pyproject.toml — without one, the
    pin-bump step has nothing to edit.
    """
    if not (app_path / "pyproject.toml").exists():
        print(f"  No root pyproject.toml in {app_name} — deferring scottycore-upgrade.yml")
        return False

    src = CORE_DIR / ".claude" / "templates" / "scottycore-upgrade.yml"
    if not src.exists():
        print(f"  template missing at {src}")
        return False

    workflows_dir = app_path / ".forgejo" / "workflows"
    dest = workflows_dir / "scottycore-upgrade.yml"
    workflows_dir.mkdir(parents=True, exist_ok=True)

    rendered = src.read_text().replace("__APP_NAME__", app_name).replace("__BRANCH__", branch)
    dest.write_text(rendered)
    print(f"  installed .forgejo/workflows/scottycore-upgrade.yml in {app_name}")
    return True


def add_to_release_apps(app_name: str) -> bool:
    """Add app to scottycore's .forgejo/workflows/release.yml APPS fan-out list.

    The release workflow dispatches `scottycore-upgrade` to every app in
    this list on every tagged release. Idempotent — no-op if already present.
    """
    release_yml = CORE_DIR / ".forgejo" / "workflows" / "release.yml"
    if not release_yml.exists():
        print(f"  release.yml not found at {release_yml}")
        return False

    text = release_yml.read_text()

    m = re.search(r"(APPS:\s*>-\n)((?:[ \t]+\S+\n)+)", text)
    if not m:
        print(f"  Could not locate APPS: block in release.yml")
        return False

    block = m.group(2)
    tokens = block.split()
    if app_name in tokens:
        print(f"  {app_name} already in release.yml APPS")
        return False

    first_line = block.splitlines()[0]
    indent = first_line[: len(first_line) - len(first_line.lstrip())]
    new_block = block.rstrip() + f"\n{indent}{app_name}\n"
    text = text[: m.start(2)] + new_block + text[m.end(2) :]

    release_yml.write_text(text)
    print(f"  Added {app_name} to release.yml APPS fan-out list")
    return True


def install_promote_nudge_hook(app_path: Path, app_name: str) -> bool:
    """Install the pre-commit /promote nudge hook into the app's .git/hooks.

    The hook runs `claude -p` on staged diffs and suggests /promote
    invocations when code looks extractable. Never blocks commits.
    Idempotent — overwrites an existing hook if we previously installed one.
    """
    src = CORE_DIR / ".claude" / "hooks" / "pre-commit-promote-nudge.sh"
    hooks_dir = app_path / ".git" / "hooks"
    dest = hooks_dir / "pre-commit"

    if not src.exists():
        print(f"  hook source missing at {src}")
        return False
    if not hooks_dir.exists():
        print(f"  {hooks_dir} does not exist — skipping hook install")
        return False

    # If an existing hook is present and wasn't installed by us, back it up
    marker = "pre-commit-promote-nudge"
    if dest.exists():
        existing = dest.read_text(errors="replace")
        if marker not in existing:
            backup = hooks_dir / "pre-commit.pre-scottycore.bak"
            dest.rename(backup)
            print(f"  existing pre-commit hook backed up to {backup.name}")

    # Write a wrapper that calls our hook and preserves the user's ability
    # to add more logic later (symlink would be brittle if the user edits it).
    wrapper = f"""#!/usr/bin/env bash
# pre-commit (installed by scottycore-init.py — {marker})
# Calls the shared nudge hook from scottycore. Never blocks the commit.
{src}
exit 0
"""
    dest.write_text(wrapper)
    dest.chmod(0o755)
    print(f"  installed pre-commit /promote nudge hook in {app_name}")
    return True


def commit_app_changes(app_path: Path, app_name: str) -> bool:
    """Commit the generated files in the app repo."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(app_path)] + list(args),
            capture_output=True, text=True, timeout=30,
        )

    # Stage the files we generated
    git("add", "CLAUDE.md",
        ".forgejo/workflows/promote-scan.yml",
        ".forgejo/workflows/scottycore-upgrade.yml",
        "docker-compose.yml",
        "pyproject.toml")

    # Check if there's anything to commit
    status = git("diff", "--cached", "--stat")
    if not status.stdout.strip():
        print(f"  No changes to commit in {app_name}")
        return False

    result = git("commit", "-m", f"feat: wire {app_name} into ScottyCore pipeline\n\n"
                 f"Added CLAUDE.md Pipeline section, promote-scan.yml workflow,\n"
                 f"and scottycore dependency pin. Generated by scottycore-init.py.")

    if result.returncode == 0:
        print(f"  Committed in {app_name}")
        return True
    else:
        print(f"  Commit failed: {result.stderr.strip()[:200]}")
        return False


def push_app(app_path: Path, branch: str, force: bool = False) -> bool:
    """Push app repo to all remotes. `force` is used on first onboarding
    to overwrite any stale history on freshly-created remotes (e.g. a
    reused GitHub repo that held commits from a prior attempt).
    """
    cmd = ["git", "-C", str(app_path), "push", "-u", "origin", branch]
    if force:
        cmd.insert(cmd.index("push") + 1, "--force")
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 0:
        print(f"  Pushed to all remotes")
        return True
    else:
        print(f"  Push failed: {result.stderr.strip()[:200]}")
        return False


def commit_scottycore_changes(app_name: str) -> bool:
    """Commit the scottycore-side changes (apps.yaml, agent file)."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(CORE_DIR)] + list(args),
            capture_output=True, text=True, timeout=30,
        )

    git("add", "config/apps.yaml",
        f".claude/agents/{app_name}-manager.md",
        ".forgejo/workflows/release.yml")

    status = git("diff", "--cached", "--stat")
    if not status.stdout.strip():
        print(f"  No scottycore changes to commit")
        return False

    result = git("commit", "-m",
                 f"feat: register {app_name} in app registry + manager agent")

    if result.returncode == 0:
        push = subprocess.run(
            ["git", "-C", str(CORE_DIR), "push"],
            capture_output=True, text=True, timeout=60,
        )
        if push.returncode == 0:
            print(f"  Committed and pushed scottycore changes")
        else:
            print(f"  Committed but push failed: {push.stderr.strip()[:200]}")
        return True
    else:
        print(f"  ScottyCore commit failed: {result.stderr.strip()[:200]}")
        return False


# ── Scaffold new app from scottycore template ───────────────────────────────


def scaffold_new_app(app_path: Path, app_name: str) -> bool:
    """Create a new app directory by cloning scottycore as a template.

    Mirrors the manual steps from scottycore/CLAUDE.md:
      1. Copy scottycore tree (minus .git, venv, caches, data, db files)
      2. Rename pyproject.toml project name to app_name
      3. `git init` + initial commit
    """
    import shutil

    if app_path.exists():
        print(f"  {app_path} already exists — skipping scaffold")
        return False

    app_path.parent.mkdir(parents=True, exist_ok=True)

    ignore_names = {
        ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
        ".mypy_cache", ".ruff_cache", "node_modules", "data",
        "app.db", "app.db-journal", ".env",
    }

    def _ignore(_dir, names):
        return [n for n in names if n in ignore_names or n.endswith(".pyc")]

    print(f"  Copying scottycore template to {app_path}")
    shutil.copytree(CORE_DIR, app_path, ignore=_ignore)

    # Update pyproject.toml project name if present
    pyproject = app_path / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text()
        updated = re.sub(
            r'(?m)^name\s*=\s*"[^"]+"',
            f'name = "{app_name}"',
            text,
            count=1,
        )
        if updated != text:
            pyproject.write_text(updated)
            print(f"  Set pyproject.toml name = \"{app_name}\"")

    # Fresh git repo
    subprocess.run(["git", "-C", str(app_path), "init", "-b", "master"],
                   capture_output=True, text=True, timeout=15)
    subprocess.run(["git", "-C", str(app_path), "add", "-A"],
                   capture_output=True, text=True, timeout=30)
    r = subprocess.run(
        ["git", "-C", str(app_path), "commit", "-m",
         f"chore: scaffold {app_name} from scottycore template"],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode == 0:
        print(f"  Initialized git repo with scaffold commit")
    else:
        print(f"  git init/commit warning: {r.stderr.strip()[:200]}")
    return True


# ── Interactive pattern selection ────────────────────────────────────────────


# ── Helpers ──────────────────────────────────────────────────────────────────


def detect_branch(app_path: Path) -> str:
    """Detect the current branch of a git repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(app_path), "branch", "--show-current"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "master"


def detect_stack(app_path: Path) -> str:
    """Try to detect the app's stack from common files."""
    if (app_path / "pyproject.toml").exists():
        content = (app_path / "pyproject.toml").read_text()
        if "fastapi" in content.lower():
            return "FastAPI"
        if "flask" in content.lower():
            return "Flask"
        if "django" in content.lower():
            return "Django"
        return "Python"
    if (app_path / "requirements.txt").exists():
        content = (app_path / "requirements.txt").read_text().lower()
        if "fastapi" in content:
            return "FastAPI"
        if "flask" in content:
            return "Flask"
        return "Python"
    if (app_path / "package.json").exists():
        return "Node.js"
    if (app_path / "Cargo.toml").exists():
        return "Rust"
    if (app_path / "go.mod").exists():
        return "Go"
    ps_files = list(app_path.glob("*.ps1")) + list(app_path.glob("*.psm1"))
    if ps_files:
        return "PowerShell"
    if (app_path / "Dockerfile").exists() or (app_path / "docker-compose.yml").exists():
        return "Docker"
    return "unknown"


# ── CLI ──────────────────────────────────────────────────────────────────────


def main():
    args = sys.argv[1:]

    if "--list" in args:
        existing = load_apps_config()
        print(f"\nRegistered apps ({len(existing)}):")
        for name, cfg in existing.items():
            core_tag = " [CORE]" if cfg.get("is_core") else ""
            print(f"  {name}: {cfg.get('path', '?')} ({cfg.get('stack', '?')}){core_tag}")
        return

    if not args or args[0].startswith("--"):
        print(__doc__)
        return

    app_path = Path(args[0]).resolve()

    # Parse flags
    app_name = None
    stack = None
    github_repo = None
    domain = None
    port_override = None
    skip_forgejo = "--skip-forgejo" in args
    skip_infra = "--skip-infra" in args
    scaffold_flag = "--scaffold" in args

    for i, arg in enumerate(args):
        if arg == "--name" and i + 1 < len(args):
            app_name = args[i + 1]
        if arg == "--stack" and i + 1 < len(args):
            stack = args[i + 1]
        if arg == "--github-repo" and i + 1 < len(args):
            github_repo = args[i + 1]
        if arg == "--domain" and i + 1 < len(args):
            domain = args[i + 1]
        if arg == "--port" and i + 1 < len(args):
            port_override = int(args[i + 1])

    if app_name is None:
        app_name = app_path.name.lower().replace(" ", "-")

    # Scaffold the app directory if it doesn't exist yet
    if not app_path.exists():
        if scaffold_flag:
            do_scaffold = True
        elif sys.stdin.isatty():
            ans = input(
                f"Path does not exist: {app_path}\n"
                f"Scaffold a new app from scottycore template? [Y/n]: "
            ).strip().lower()
            do_scaffold = ans in ("", "y", "yes")
        else:
            print(f"Error: path does not exist: {app_path} "
                  f"(pass --scaffold to create it)")
            sys.exit(1)

        if not do_scaffold:
            print("Aborted — no changes made.")
            sys.exit(1)

        print(f"\nStep 0: Scaffolding new app at {app_path}")
        scaffold_new_app(app_path, app_name)

    if stack is None:
        stack = detect_stack(app_path)

    branch = detect_branch(app_path)

    print(f"\n{'='*60}")
    print(f"  ScottyCore Init — Onboarding '{app_name}'")
    print(f"{'='*60}")
    print(f"  Path:    {app_path}")
    print(f"  Stack:   {stack}")
    print(f"  Branch:  {branch}")
    print()

    # Step 1: Forgejo repo + dual-remote
    print("Step 1: Git remotes (Forgejo + GitHub)")
    github_url = detect_github_remote(app_path)
    if github_url:
        print(f"  Found GitHub remote: {github_url}")
    elif github_repo:
        github_url = f"https://github.com/{github_repo}.git"
        print(f"  Using provided GitHub repo: {github_url}")
    else:
        github_url = f"https://github.com/{GITHUB_USER}/{app_name}.git"
        print(f"  Assuming GitHub repo: {github_url}")

    # GitHub repo (idempotent; needed before first dual-push)
    if not detect_github_remote(app_path):
        create_github_repo(app_name, description=f"Scotty app: {stack}")

    if not skip_forgejo:
        forgejo_url = create_forgejo_repo(
            app_name,
            description=f"Scotty app: {stack}",
            default_branch=branch,
        )
        if forgejo_url:
            setup_dual_remote(app_path, forgejo_url, github_url, branch)
            # Push FORGEJO_TOKEN to the new repo so its workflows can
            # create PRs, dispatch workflows, and post commit comments.
            token = _read_forgejo_token()
            if token:
                set_forgejo_secret(app_name, "FORGEJO_TOKEN", token)
    else:
        print(f"  --skip-forgejo: skipping Forgejo repo creation")

    # Port assignment + FQDN. Reuse existing port on re-runs so idempotency
    # doesn't shift ports out from under the deployed container.
    existing_apps = load_apps_config()
    if port_override is not None:
        port = port_override
    elif app_name in existing_apps and "port" in existing_apps[app_name]:
        port = int(existing_apps[app_name]["port"])
    else:
        port = pick_next_port(existing_apps)
    if domain:
        fqdn = domain
    elif app_name in existing_apps and "fqdn" in existing_apps[app_name]:
        fqdn = existing_apps[app_name]["fqdn"]
    else:
        fqdn = f"{app_name}.{DEFAULT_DOMAIN_APEX}"
    apex = fqdn.split(".", 1)[1] if "." in fqdn else fqdn

    # Step 2: Register
    print(f"\nStep 2: Register in config/apps.yaml")
    register_app(app_path, app_name, stack, branch, port, fqdn)

    # Step 2b: Emit docker-compose.yml into the scaffolded app
    print(f"\nStep 2b: Render docker-compose.yml into app")
    install_docker_compose(app_path, app_name, port)

    # Step 3: Manager agent
    print(f"\nStep 3: Scaffold manager agent")
    scaffold_agent(app_name, app_path, stack)

    # Step 4: CLAUDE.md injection
    print(f"\nStep 4: Inject ScottyCore Pipeline section into CLAUDE.md")
    inject_claude_section(app_path)

    # Step 5a: Install pre-commit nudge hook (early-warning, local)
    print(f"\nStep 5a: Install pre-commit /promote nudge hook")
    install_promote_nudge_hook(app_path, app_name)

    # Step 5b: Install server-side promote-scan workflow (authoritative upward)
    print(f"\nStep 5b: Install promote-scan.yml workflow (upward pipeline)")
    install_promote_scan_workflow(app_path, app_name)

    # Step 5c: Install scottycore-upgrade workflow (downward pipeline)
    print(f"\nStep 5c: Install scottycore-upgrade.yml workflow (downward pipeline)")
    upgrade_installed = install_scottycore_upgrade_workflow(app_path, app_name, branch)

    # Step 5d: Add to scottycore release.yml APPS fan-out (only if upgrade.yml installed)
    if upgrade_installed:
        print(f"\nStep 5d: Add {app_name} to scottycore release.yml APPS")
        add_to_release_apps(app_name)

    # Step 5e: Provision LXC on proxmox1 (idempotent — reuses if hostname matches)
    lxc_ip = None
    lxc_vmid = None
    if not skip_infra:
        print(f"\nStep 5e: Provision LXC on {PROXMOX_NODE_NAME}")
        prov = provision_lxc(app_name)
        if prov:
            lxc_vmid, lxc_ip = prov

    # Step 5f: Declare infra in scottylab (cert apex, nginx vhost, scottycore-apps, inventory)
    if not skip_infra:
        print(f"\nStep 5f: Declare infra in scottylab")
        target_host = f"{app_name}.melbourne"
        repo_url = f"{FORGEJO_BASE}/{FORGEJO_USER}/{app_name}.git"
        upstream = lxc_ip or target_host  # real IP if we have it; else placeholder
        add_nginx_cert_apex(apex)
        add_nginx_vhost(app_name, fqdn, apex, upstream, port)
        add_scottycore_app(app_name, target_host, port, repo_url, branch)
        if lxc_ip and lxc_vmid is not None:
            register_in_inventory(app_name, target_host, lxc_ip, lxc_vmid,
                                  note=f"scottycore app — {stack}")
        commit_scottylab_changes(app_name)

    # Step 5g: Publish via Cloudflare (DNS CNAME + tunnel ingress)
    if not skip_infra:
        print(f"\nStep 5g: Publish {fqdn} via Cloudflare tunnel")
        ensure_cf_cname(fqdn, apex)
        ensure_tunnel_ingress(fqdn)

    # Step 5g2: UniFi local DNS — point LAN clients straight at nginx for fqdn.
    # Skipped silently if secrets/unifi.yml is missing or still a placeholder.
    # Stateful: no-op if an existing *.<parent> wildcard already covers fqdn.
    if not skip_infra:
        print(f"\nStep 5g2: UniFi gateway DNS for {fqdn}")
        ensure_unifi_dns(fqdn, a_value=NGINX_HOST)

    # Step 5g3: Reconcile UniFi wildcards against nginx-certs.yml — every zone
    # nginx serves should have a matching *.<zone> A+AAAA pair on the gateway.
    # Declarative: deletions on the gateway get healed on any re-run.
    if not skip_infra:
        print(f"\nStep 5g3: UniFi wildcard sync (zones from nginx-certs.yml)")
        sync_unifi_wildcards()


    # Step 6: Commit + push app changes
    print(f"\nStep 6: Commit and push app changes")
    committed = commit_app_changes(app_path, app_name)
    if committed:
        # Force on first onboarding: the remotes were just provisioned
        # (or reused), and the scaffold is the authoritative starting point.
        push_app(app_path, branch, force=True)

    # Step 7: Commit + push scottycore changes
    print(f"\nStep 7: Commit and push scottycore changes")
    commit_scottycore_changes(app_name)

    # Step 8: Run ansible — install Docker on new LXC, issue cert, deploy, publish vhost
    if not skip_infra and lxc_ip:
        print(f"\nStep 8: Run ansible end-to-end")
        target_host = f"{app_name}.melbourne"
        install_docker_on(target_host)
        issue_certs()
        deploy_scottycore_app(target_host)
        publish_vhost()

    print(f"\n{'='*60}")
    print(f"  Done! '{app_name}' is now part of the ScottyCore ecosystem.")
    print(f"{'='*60}")
    print(f"\n  What happened:")
    print(f"  - Forgejo repo created at {FORGEJO_BASE}/{FORGEJO_USER}/{app_name}")
    print(f"  - Dual-remote configured (push to Forgejo + GitHub)")
    print(f"  - Registered in config/apps.yaml")
    print(f"  - Manager agent created in scottycore/.claude/agents/")
    print(f"  - ScottyCore Pipeline section injected into CLAUDE.md")
    print(f"  - Pre-commit /promote nudge hook installed")
    print(f"  - .forgejo/workflows/promote-scan.yml installed (upward)")
    print(f"  - .forgejo/workflows/scottycore-upgrade.yml installed (downward)")
    print(f"  - FORGEJO_TOKEN set as repo-scoped Actions secret")
    print(f"  - {app_name} added to scottycore release.yml APPS fan-out")
    print(f"  - docker-compose.yml rendered (port {port})")
    if not skip_infra:
        if lxc_ip and lxc_vmid is not None:
            print(f"  - Provisioned LXC CT {lxc_vmid} on {PROXMOX_NODE_NAME} at {lxc_ip}")
            print(f"  - Registered {app_name}.melbourne in scottylab inventory")
        print(f"  - scottylab: {apex} added to nginx-certs.yml (if new apex)")
        print(f"  - scottylab: {fqdn} -> http://{lxc_ip or app_name + '.melbourne'}:{port} in nginx-vhosts.yml")
        print(f"  - scottylab: {app_name} added to scottycore-apps.yml deploy list")
        print(f"  - Cloudflare: {fqdn} CNAME -> melbourne tunnel + ingress rule")
        print(f"  - UniFi: {fqdn} A/AAAA (or wildcard coverage confirmed)")
        print(f"  - UniFi: wildcard *.<zone> A+AAAA for every nginx-certs zone")
        print(f"  - Ansible: docker installed, cert issued, container deployed, vhost published")
    print(f"  - All changes committed and pushed")
    print(f"\n  Verify:  curl -sSf https://{fqdn}/health")
    print()


if __name__ == "__main__":
    main()
