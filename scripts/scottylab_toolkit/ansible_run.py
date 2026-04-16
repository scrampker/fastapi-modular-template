"""Thin ansible-playbook runner. Lives next to the toolkit for callers that
don't want to shell out manually. Only parses recap lines for brevity.
"""

import re
import subprocess

from .paths import ANSIBLE_DIR


def run(playbook: str, *, limit: str | None = None,
        extra_vars: dict | None = None, timeout: int = 600) -> bool:
    """Run an ansible playbook under `playbooks/workloads/` with the
    standard `hosts.yml + workloads.yml` inventory pair.

    On failure, prints the last 20 lines. On success, prints per-host recap
    lines (`host : ok=N changed=N ...`) so callers see what happened.
    """
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
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        tail = (r.stdout + r.stderr).splitlines()[-20:]
        print(f"  ansible {playbook} FAILED:")
        for ln in tail:
            print(f"    {ln}")
        return False

    recap = [ln for ln in r.stdout.splitlines()
             if re.search(r"\s:\s+ok=\d+", ln)]
    for ln in recap:
        print(f"  {ln.strip()}")
    return True


def install_docker(host: str) -> bool:
    print(f"  installing Docker on {host}...")
    return run("docker.yml", limit=host, extra_vars={"target_hosts": "all"})


def issue_certs() -> bool:
    print(f"  issuing/expanding Let's Encrypt certs...")
    return run("nginx-certs.yml")


def deploy_scottycore_app(host: str) -> bool:
    print(f"  deploying scottycore app on {host}...")
    return run("scottycore-apps.yml", limit=host,
               extra_vars={"target_hosts": "all"})


def publish_vhost() -> bool:
    print(f"  publishing nginx vhost...")
    return run("nginx-vhosts.yml")
