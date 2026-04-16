"""UniFi OS local gateway: static DNS record management.

Source of creds: `automation/ansible/secrets/unifi.yml` (gitignored; template
at `unifi.yml.example`). Never commits secrets. Callers that skip silently
when the password is still the `<paste-...>` placeholder get non-fatal
degradation — good for running in CI-like contexts without real creds.

Reads the CSRF token from the `X-CSRF-Token` login response header — NOT
from a cookie; the TOKEN cookie is a JWT envelope. Without this, writes
return 403.
"""

import http.cookiejar
import json
import ssl
import urllib.error
import urllib.request

from .paths import UNIFI_DEFAULT_AAAA, UNIFI_SECRETS_PATH

UNIFI_STATIC_DNS_PATH = "/proxy/network/v2/api/site/{site}/static-dns"


def load_creds() -> dict | None:
    """Read secrets/unifi.yml. Returns None on missing file / placeholder
    password / parse error — callers should treat that as a skip, not fail.
    """
    if not UNIFI_SECRETS_PATH.exists():
        return None
    try:
        import yaml
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
        print(f"  unifi.yml password still has a placeholder — skipping UniFi")
        return None
    return u


def login(u: dict):
    """Authenticate against UniFi OS. Returns (headers, opener) or None.

    The returned `opener` carries the session cookies; `headers` contains
    the CSRF token that must accompany every write call.
    """
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
        "username": u["username"], "password": u["password"],
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
            # The TOKEN cookie is a JWT — we don't parse it. CSRF comes
            # from the X-CSRF-Token response header.
            csrf = (resp.headers.get("X-CSRF-Token")
                    or resp.headers.get("X-Updated-CSRF-Token")
                    or "")
    except Exception as e:
        print(f"  UniFi login failed: {e}")
        return None

    headers = {"X-CSRF-Token": csrf} if csrf else {}
    return headers, opener


def request(u: dict, opener, method: str, path: str,
            headers: dict, body: dict | None = None):
    """Thin wrapper around opener.open with standard error reporting."""
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
    """True iff a pre-existing `*.<parent>` record with matching type+value
    already resolves `fqdn`. Walks every proper parent — for a.b.c.d.com we
    check *.b.c.d.com, *.c.d.com, *.d.com (but not *.com).
    """
    parts = fqdn.split(".")
    for i in range(1, len(parts) - 1):
        wildcard = "*." + ".".join(parts[i:])
        for r in records:
            if (r.get("key") == wildcard
                    and r.get("record_type") == rec_type
                    and r.get("value") == value
                    and r.get("enabled", True)):
                return True
    return False


def ensure_dns(fqdn: str, a_value: str,
               aaaa_value: str = UNIFI_DEFAULT_AAAA) -> bool:
    """Upsert A + AAAA records for `fqdn`.

    Stateful rules per record type:
      1. Wildcard coverage: if a `*.<parent>` with same type+value exists,
         skip (the wildcard already resolves fqdn correctly).
      2. Specific with right value: skip.
      3. Specific with wrong value: update.
      4. Otherwise: create.
    """
    u = load_creds()
    if u is None:
        return False
    auth = login(u)
    if auth is None:
        return False
    headers, opener = auth
    site = u.get("site", "default")
    path = UNIFI_STATIC_DNS_PATH.format(site=site)

    records = request(u, opener, "GET", path, headers)
    if records is None:
        return False

    def upsert(rec_type: str, value: str) -> None:
        if _wildcard_covers(records, fqdn, rec_type, value):
            print(f"  UniFi: {fqdn} {rec_type} covered by wildcard ({value})")
            return
        existing = [r for r in records
                    if r.get("key") == fqdn and r.get("record_type") == rec_type]
        if existing:
            cur = existing[0]
            if cur.get("value") == value and cur.get("enabled", True):
                print(f"  UniFi: {fqdn} {rec_type} already = {value}")
                return
            body = {**cur, "value": value, "enabled": True}
            r = request(u, opener, "PUT", f"{path}/{cur['_id']}", headers, body)
            if r is not None:
                print(f"  UniFi: updated {fqdn} {rec_type} = {value}")
            return
        body = {
            "key": fqdn, "record_type": rec_type, "value": value,
            "enabled": True,
            "ttl": 0, "port": 0, "priority": 0, "weight": 0,
        }
        r = request(u, opener, "POST", path, headers, body)
        if r is not None:
            print(f"  UniFi: created {fqdn} {rec_type} = {value}")

    upsert("A", a_value)
    upsert("AAAA", aaaa_value)
    return True


def sync_wildcards(zones: list[str], nginx_ip: str,
                   aaaa_value: str = UNIFI_DEFAULT_AAAA) -> bool:
    """Ensure every `zone` in the list has `*.<zone>` A+AAAA records.

    Purely declarative: zones list is the source of truth; gateway state
    is reconciled to match. Deleted records get recreated on every run.
    """
    u = load_creds()
    if u is None:
        return False
    auth = login(u)
    if auth is None:
        return False
    headers, opener = auth
    site = u.get("site", "default")
    path = UNIFI_STATIC_DNS_PATH.format(site=site)

    records = request(u, opener, "GET", path, headers)
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
            r = request(u, opener, "PUT", f"{path}/{cur['_id']}", headers, body)
            if r is not None:
                print(f"  UniFi: updated {key} {rec_type} = {value}")
                updated += 1
            return
        body = {
            "key": key, "record_type": rec_type, "value": value,
            "enabled": True,
            "ttl": 0, "port": 0, "priority": 0, "weight": 0,
        }
        r = request(u, opener, "POST", path, headers, body)
        if r is not None:
            print(f"  UniFi: created {key} {rec_type} = {value}")
            created += 1

    for zone in zones:
        upsert_wildcard(zone, "A", nginx_ip)
        upsert_wildcard(zone, "AAAA", aaaa_value)

    print(f"  UniFi wildcard sync: {len(zones)} zones, "
          f"{created} created, {updated} updated, {unchanged} unchanged")
    return True
