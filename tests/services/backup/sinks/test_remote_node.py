"""RemoteNodeSink tests — monkeypatches asyncio.create_subprocess_exec."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone

import pytest

from scottycore.services.backup.sinks import (
    BackupBlob,
    RemoteNodeSink,
    SinkNotFoundError,
)


def _blob(data: bytes) -> BackupBlob:
    return BackupBlob(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        app_slug="demo",
        scope="platform",
        kind="full",
        created_at=datetime(2026, 4, 16, 12, 0, tzinfo=timezone.utc),
    )


class _FakeProcess:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


@pytest.fixture
def stubbed_subprocess(monkeypatch, tmp_path):
    """Replace asyncio.create_subprocess_exec with a driver we can script.

    The fake interprets scp/ssh commands against a local tmp_path "remote".
    """
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    calls: list[list[str]] = []

    async def fake_exec(*argv, stdout=None, stderr=None, **kwargs):
        args = list(argv)
        calls.append(args)
        prog = args[0]

        if prog == "ssh":
            # Extract remote argv after the "--" separator.
            try:
                sep = args.index("--")
            except ValueError:
                return _FakeProcess(0)
            remote_argv = args[sep + 1 :]
            # Tokens come in shlex-quoted; strip a single layer of quotes.
            tokens = [_unquote(t) for t in remote_argv]

            if tokens[:2] == ["mkdir", "-p"]:
                (remote_root / tokens[2].lstrip("/")).mkdir(parents=True, exist_ok=True)
                return _FakeProcess(0)
            if tokens[:2] == ["test", "-f"]:
                target = remote_root / tokens[2].lstrip("/")
                return _FakeProcess(0 if target.is_file() else 1)
            if tokens[0] == "find":
                # find <dir> -name *.meta.json -type f
                base = remote_root / tokens[1].lstrip("/")
                matches = list(base.rglob("*.meta.json"))
                out = "\n".join(
                    "/" + str(m.relative_to(remote_root)) for m in matches
                ).encode()
                return _FakeProcess(0, stdout=out)
            if tokens[0] == "cat":
                p = remote_root / tokens[1].lstrip("/")
                return _FakeProcess(0, stdout=p.read_bytes() if p.exists() else b"")
            if tokens[0] == "rm":
                # rm -f path meta-path
                for arg in tokens[2:]:
                    p = remote_root / arg.lstrip("/")
                    if p.exists():
                        p.unlink()
                return _FakeProcess(0)

            return _FakeProcess(0)

        if prog == "scp":
            # scp src dest (src or dest may be "host:/abs/path")
            src, dest = args[-2], args[-1]
            if ":" in dest:  # put
                local, remote = src, dest.split(":", 1)[1]
                target = remote_root / remote.lstrip("/")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(_read(local))
            else:  # get
                remote = src.split(":", 1)[1]
                source = remote_root / remote.lstrip("/")
                from pathlib import Path as _P

                _P(dest).write_bytes(source.read_bytes())
            return _FakeProcess(0)

        return _FakeProcess(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return remote_root, calls


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] == "'":
        return s[1:-1]
    return s


def _read(path: str) -> bytes:
    from pathlib import Path as _P

    return _P(path).read_bytes()


@pytest.mark.asyncio
async def test_put_uploads_via_scp(stubbed_subprocess) -> None:
    remote_root, _ = stubbed_subprocess
    sink = RemoteNodeSink(host="node.test", remote_dir="/backups", user="ops")

    result = await sink.put(_blob(b"hello"))

    assert result.sink_type == "remote_node"
    target = remote_root / "backups" / result.locator
    assert target.read_bytes() == b"hello"
    sidecar = target.with_suffix(target.suffix + ".meta.json")
    assert json.loads(sidecar.read_text())["sha256"] == _blob(b"hello").sha256


@pytest.mark.asyncio
async def test_get_round_trip(stubbed_subprocess) -> None:
    _, _ = stubbed_subprocess
    sink = RemoteNodeSink(host="node.test", remote_dir="/backups", user="ops")
    r = await sink.put(_blob(b"abc"))
    assert await sink.get(r.locator) == b"abc"


@pytest.mark.asyncio
async def test_get_missing_raises(stubbed_subprocess) -> None:
    sink = RemoteNodeSink(host="node.test", remote_dir="/backups", user="ops")
    with pytest.raises(SinkNotFoundError):
        await sink.get("nope.tar.gz")


@pytest.mark.asyncio
async def test_list_and_delete(stubbed_subprocess) -> None:
    sink = RemoteNodeSink(host="node.test", remote_dir="/backups", user="ops")
    b1 = BackupBlob(
        data=b"one",
        sha256=hashlib.sha256(b"one").hexdigest(),
        size=3,
        app_slug="demo",
        scope="platform",
        kind="full",
        created_at=datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc),
    )
    b2 = BackupBlob(
        data=b"two",
        sha256=hashlib.sha256(b"two").hexdigest(),
        size=3,
        app_slug="demo",
        scope="platform",
        kind="full",
        created_at=datetime(2026, 4, 16, 12, 0, tzinfo=timezone.utc),
    )
    r1 = await sink.put(b1)
    await sink.put(b2)

    snaps = await sink.list_snapshots()
    assert len(snaps) == 2

    await sink.delete(r1.locator)
    snaps2 = await sink.list_snapshots()
    assert len(snaps2) == 1


@pytest.mark.asyncio
async def test_ssh_options_propagate(stubbed_subprocess) -> None:
    _, calls = stubbed_subprocess
    sink = RemoteNodeSink(
        host="node.test",
        remote_dir="/backups",
        user="ops",
        port=2222,
        ssh_key="/tmp/key",
        ssh_options=["StrictHostKeyChecking=no"],
    )
    await sink.put(_blob(b"x"))

    ssh_calls = [c for c in calls if c[0] == "ssh"]
    assert ssh_calls, "expected at least one ssh invocation"
    flat = " ".join(ssh_calls[0])
    assert "-p 2222" in flat
    assert "-i /tmp/key" in flat
    assert "StrictHostKeyChecking=no" in flat
