"""scottylab — command-line entry point.

Thin wrapper around the primitives in scottylab_toolkit. Use this to reach
individual operations without going through scottycore-init.py.

Subcommands:
  publish <fqdn> --upstream <host:port> [--no-unifi] [--no-tunnel]
      End-to-end publish: CF CNAME + tunnel ingress + UniFi DNS + nginx
      vhost. Does NOT provision an LXC — bring your own upstream.

  dns-sync
      Reconcile UniFi gateway wildcards against nginx-certs.yml zones.

  dns-upsert <fqdn> <ipv4>
      Upsert a single UniFi A (+ AAAA ::dead:beef) record for fqdn.

  provision-lxc <hostname>
      Create an unprivileged Docker-capable LXC on the default Proxmox
      node. Prints (vmid, ip).

  zones
      Print every DNS zone served by nginx, as derived from nginx-certs.yml.
"""

import argparse
import sys

from . import ansible_run, cloudflare, inventory, lxc, nginx, unifi
from .paths import NGINX_HOST


def _cmd_publish(args: argparse.Namespace) -> int:
    fqdn = args.fqdn
    apex = fqdn.split(".", 1)[1] if "." in fqdn else fqdn
    upstream_host, _, upstream_port = args.upstream.partition(":")
    if not upstream_port:
        print(f"error: --upstream must be host:port (got {args.upstream!r})")
        return 2
    name = args.name or fqdn.split(".")[0]

    print(f"Publishing {fqdn} -> http://{upstream_host}:{upstream_port}")
    print()

    print("[1/4] nginx-vhosts.yml")
    nginx.add_vhost(name, fqdn, apex, upstream_host, int(upstream_port))

    print("[2/4] Cloudflare DNS + tunnel ingress")
    if not args.no_tunnel:
        cloudflare.ensure_cname(fqdn, apex)
        cloudflare.ensure_tunnel_ingress(fqdn)
    else:
        print("  skipped (--no-tunnel)")

    print("[3/4] UniFi gateway DNS")
    if not args.no_unifi:
        unifi.ensure_dns(fqdn, a_value=NGINX_HOST)
    else:
        print("  skipped (--no-unifi)")

    print("[4/4] Reload nginx via ansible")
    if not args.no_reload:
        ansible_run.publish_vhost()
    else:
        print("  skipped (--no-reload)")

    print()
    print(f"Done. Verify: curl -sSf https://{fqdn}/")
    return 0


def _cmd_dns_sync(_args: argparse.Namespace) -> int:
    zones = nginx.cert_zones()
    if not zones:
        print("No zones found in nginx-certs.yml")
        return 1
    print(f"Reconciling {len(zones)} zones from nginx-certs.yml -> UniFi")
    ok = unifi.sync_wildcards(zones, NGINX_HOST)
    return 0 if ok else 1


def _cmd_dns_upsert(args: argparse.Namespace) -> int:
    ok = unifi.ensure_dns(args.fqdn, a_value=args.ip)
    return 0 if ok else 1


def _cmd_provision_lxc(args: argparse.Namespace) -> int:
    result = lxc.provision(args.hostname, cores=args.cores,
                           memory_mb=args.memory, disk_gb=args.disk)
    if not result:
        return 1
    vmid, ip = result
    if args.register:
        inventory.register(f"{args.hostname}.melbourne", ip, vmid,
                           note=args.note or "")
    print(f"vmid={vmid}")
    print(f"ip={ip}")
    return 0


def _cmd_zones(_args: argparse.Namespace) -> int:
    for z in nginx.cert_zones():
        print(z)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scottylab",
        description="Scotty homelab infrastructure toolkit",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pub = sub.add_parser("publish", help="publish an fqdn end-to-end")
    pub.add_argument("fqdn", help="fully qualified domain name (e.g. foo.corpaholics.com)")
    pub.add_argument("--upstream", required=True,
                     help="upstream host:port (e.g. 192.168.150.221:8100)")
    pub.add_argument("--name", help="vhost slug (default: fqdn's first label)")
    pub.add_argument("--no-unifi", action="store_true",
                     help="skip UniFi gateway DNS")
    pub.add_argument("--no-tunnel", action="store_true",
                     help="skip Cloudflare CNAME + tunnel ingress")
    pub.add_argument("--no-reload", action="store_true",
                     help="skip ansible nginx-vhosts.yml reload")
    pub.set_defaults(func=_cmd_publish)

    ds = sub.add_parser("dns-sync",
                        help="reconcile UniFi wildcards against nginx-certs.yml")
    ds.set_defaults(func=_cmd_dns_sync)

    du = sub.add_parser("dns-upsert", help="upsert a single UniFi A+AAAA record")
    du.add_argument("fqdn")
    du.add_argument("ip")
    du.set_defaults(func=_cmd_dns_upsert)

    pl = sub.add_parser("provision-lxc", help="create a new Docker-capable LXC")
    pl.add_argument("hostname")
    pl.add_argument("--cores", type=int, default=2)
    pl.add_argument("--memory", type=int, default=2048, help="MB")
    pl.add_argument("--disk", type=int, default=16, help="GB")
    pl.add_argument("--register", action="store_true",
                    help="also add to scottylab inventory/workloads.yml")
    pl.add_argument("--note", help="inventory note field")
    pl.set_defaults(func=_cmd_provision_lxc)

    z = sub.add_parser("zones",
                       help="list every zone served by nginx (from nginx-certs.yml)")
    z.set_defaults(func=_cmd_zones)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
