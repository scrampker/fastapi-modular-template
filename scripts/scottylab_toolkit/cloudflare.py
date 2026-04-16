"""Cloudflare: DNS record upsert + melbourne tunnel ingress management.

The CF DNS-01 token lives in `/etc/letsencrypt/cloudflare.ini` on the nginx
LXC. It has Zone:DNS:Edit scope — sufficient for DNS writes but NOT for
account-level tunnel configuration. Tunnel ingress is therefore managed by
SSH-editing `/etc/cloudflared/config.yml` on the nginx host and restarting
cloudflared, which is fine as long as the tunnel stays locally-managed.
"""

import json
import subprocess
import urllib.error
import urllib.request

from . import ssh
from .paths import (
    CF_API, CF_TOKEN_REMOTE_PATH, CF_TUNNEL_HOSTNAME, NGINX_HOST,
)


def token() -> str | None:
    """Read the Cloudflare API token from the nginx LXC's certbot config."""
    body = ssh.read(NGINX_HOST, CF_TOKEN_REMOTE_PATH)
    if not body:
        return None
    for line in body.splitlines():
        if "dns_cloudflare_api_token" in line:
            return line.split("=", 1)[1].strip()
    return None


def _zone_id(tok: str, apex: str) -> str | None:
    req = urllib.request.Request(
        f"{CF_API}/zones?name={apex}",
        headers={"Authorization": f"Bearer {tok}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            results = data.get("result", [])
            return results[0]["id"] if results else None
    except Exception as e:
        print(f"  CF zone lookup failed: {e}")
        return None


def ensure_cname(fqdn: str, apex: str, target: str = CF_TUNNEL_HOSTNAME,
                 *, proxied: bool = True, comment: str | None = None) -> bool:
    """Create/update CNAME `fqdn` -> `target` in Cloudflare. Idempotent."""
    tok = token()
    if not tok:
        print(f"  No Cloudflare token available — skipping DNS")
        return False
    zone_id = _zone_id(tok, apex)
    if not zone_id:
        print(f"  Cloudflare zone '{apex}' not found — skipping DNS")
        return False

    name = fqdn.removesuffix(f".{apex}") if fqdn != apex else "@"
    probe = urllib.request.Request(
        f"{CF_API}/zones/{zone_id}/dns_records?type=CNAME&name={fqdn}",
        headers={"Authorization": f"Bearer {tok}"},
    )
    try:
        with urllib.request.urlopen(probe, timeout=15) as resp:
            existing = json.loads(resp.read()).get("result", [])
    except Exception as e:
        print(f"  CF DNS probe failed: {e}")
        return False

    if existing and existing[0].get("content") == target:
        print(f"  CF CNAME {fqdn} already points to {target}")
        return True

    payload = json.dumps({
        "type": "CNAME",
        "name": name,
        "content": target,
        "proxied": proxied,
        "comment": comment or "managed by scottylab_toolkit",
    }).encode()
    method = "PUT" if existing else "POST"
    url = (f"{CF_API}/zones/{zone_id}/dns_records/{existing[0]['id']}"
           if existing else
           f"{CF_API}/zones/{zone_id}/dns_records")
    req = urllib.request.Request(
        url, data=payload, method=method,
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        if data.get("success"):
            print(f"  CF CNAME {fqdn} -> {target} ({method})")
            return True
        print(f"  CF DNS error: {data.get('errors')}")
        return False
    except urllib.error.HTTPError as e:
        print(f"  CF DNS HTTP {e.code}: {e.read().decode()[:200]}")
        return False


def ensure_tunnel_ingress(fqdn: str,
                          service: str = "http://localhost:80") -> bool:
    """Add an ingress rule `fqdn -> service` on the melbourne tunnel.

    SSH-edits `/etc/cloudflared/config.yml` on the nginx host; idempotent —
    skips if the rule already exists. Validates via `cloudflared ingress
    validate` and restarts the service.
    """
    check = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", NGINX_HOST,
         f"grep -q 'hostname: {fqdn}' /etc/cloudflared/config.yml && echo present"],
        capture_output=True, text=True, timeout=15,
    )
    if "present" in check.stdout:
        print(f"  tunnel ingress for {fqdn} already present")
        return True

    remote_cmd = f"""
set -e
CONF=/etc/cloudflared/config.yml
cp $CONF $CONF.bak.$(date +%s)
python3 - <<'PY'
import re
conf = open('/etc/cloudflared/config.yml').read()
rule = '''  - hostname: {fqdn}
    service: {service}
    originRequest:
      httpHostHeader: {fqdn}
'''
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
