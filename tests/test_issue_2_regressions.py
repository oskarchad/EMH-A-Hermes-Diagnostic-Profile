import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
ORIENTATION = ROOT / "skills/emh-orientation/SKILL.md"
BREAKGLASS = ROOT / "skills/emh-rescue-media/scripts/breakglass_collect.py"
SQLITE_HEALTH = ROOT / "skills/emh-nightly-self-check/scripts/sqlite_health.py"
NIGHTLY_SKILL = ROOT / "skills/emh-nightly-self-check/SKILL.md"
UPSTREAM_CHECK = ROOT / "skills/emh-triage/scripts/upstream_check.py"


def load_script(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _documented_cron_command(job_name: str) -> list[str]:
    orientation = ORIENTATION.read_text(encoding="utf-8")
    documented_commands = re.findall(r"`([^`\n]*hermes cron create[^`\n]*)`", orientation)
    assert documented_commands
    assert all("--prompt-file" not in command for command in documented_commands)
    line = next(
        line for line in orientation.splitlines()
        if "hermes cron create" in line and f"--name {job_name}" in line
    )
    command = line.split("`", 2)[1]
    return shlex.split(command)


def test_orientation_command_targets_explicit_store_and_preserves_delivery(tmp_path):
    parts = _documented_cron_command("emh-nightly-self-check")
    intended = tmp_path / "operator-default"
    inherited = tmp_path / "profiles" / "emh"
    intended.mkdir()
    inherited.mkdir(parents=True)

    assert parts[0].startswith("HERMES_HOME=")
    assert "default-profile-path" in parts[0]
    parts[0] = f"HERMES_HOME={intended}"
    parts[parts.index("<canonical-nightly-prompt>")] = (
        "Run the bounded read-only nightly EMH health check. Never repair."
    )
    parts[parts.index("<approved-delivery-target>")] = "local"
    parts[parts.index("hermes")] = shutil.which("hermes") or "hermes"

    env = {**os.environ, "HERMES_HOME": str(inherited)}
    env["HERMES_HOME"] = parts[0].split("=", 1)[1]
    result = subprocess.run(
        parts[1:], capture_output=True, text=True, timeout=30, env=env, check=False
    )
    assert result.returncode == 0, result.stderr or result.stdout

    intended_jobs = json.loads(
        (intended / "cron" / "jobs.json").read_text(encoding="utf-8")
    )["jobs"]
    assert len(intended_jobs) == 1
    assert intended_jobs[0]["name"] == "emh-nightly-self-check"
    assert intended_jobs[0]["deliver"] == "local"
    assert intended_jobs[0]["prompt"].startswith("Run the bounded read-only")
    assert not (inherited / "cron" / "jobs.json").exists()


def test_breakglass_output_redacts_private_paths_user_and_profile_details(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_hermes = fake_bin / "hermes"
    fake_hermes.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  \"--version\") printf 'Hermes Agent v9.9.9\\nInstall directory: /srv/private-mount/alice/hermes\\n' ;;\n"
        "  \"profile list\") printf 'default\\nsecret-client-profile\\n' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_hermes.chmod(0o755)
    private_home = "/srv/private-mount/alice/hermes-home"
    env = {
        **os.environ,
        "HOME": "/home/alice",
        "HERMES_HOME": private_home,
        "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
    }

    result = subprocess.run(
        [sys.executable, str(BREAKGLASS)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    baseline = json.loads(result.stdout)
    serialized = json.dumps(baseline, sort_keys=True)

    for private_value in (
        private_home,
        "/srv/private-mount",
        "/home/alice",
        "alice",
        "secret-client-profile",
    ):
        assert private_value not in serialized
    assert baseline["hermes"]["on_path"] is True
    assert baseline["hermes"]["version"] == "9.9.9"
    assert baseline["hermes"]["profile_count"] == 2
    assert baseline["hermes"]["home_kind"] == "custom"
    assert isinstance(baseline["disk"]["total_bytes"], int)
    assert isinstance(baseline["disk"]["free_bytes"], int)


def _create_state_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL, "
            "ended_at REAL)"
        )
        conn.executemany(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            [
                ("s1", "cron", 10.0, None),
                ("s2", "cron", 20.0, None),
                ("s3", "cli", 30.0, None),
                ("s4", "tui", 40.0, None),
                ("s5", "cli", 50.0, 60.0),
            ],
        )


def _create_executions_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE executions ("
            "id TEXT PRIMARY KEY, job_id TEXT NOT NULL, status TEXT NOT NULL, "
            "claimed_at TEXT NOT NULL, finished_at TEXT, error TEXT)"
        )
        conn.executemany(
            "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("e1", "job-a", "failed", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z", "private detail"),
                ("e2", "job-a", "completed", "2026-01-02T00:00:00Z", "2026-01-02T00:01:00Z", None),
                ("e3", "job-b", "failed", "2026-01-03T00:00:00Z", "2026-01-03T00:01:00Z", "private detail"),
                ("e4", "job-c", "running", "2026-01-04T00:00:00Z", None, None),
            ],
        )


def test_sqlite_health_queries_are_bounded_read_only_and_redacted(tmp_path):
    state_db = tmp_path / "state.db"
    executions_db = tmp_path / "executions.db"
    _create_state_db(state_db)
    _create_executions_db(executions_db)
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (state_db, executions_db)
    }
    state_db.chmod(0o444)
    executions_db.chmod(0o444)

    result = subprocess.run(
        [
            sys.executable,
            str(SQLITE_HEALTH),
            "--state-db",
            str(state_db),
            "--executions-db",
            str(executions_db),
            "--limit",
            "2",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)

    assert report["status"] == "ok"
    assert report["limit"] == 2
    assert report["open_sessions"] == {
        "rows": [
            {"source": "cron", "open_count": 2, "oldest_started_at": 10.0},
            {"source": "cli", "open_count": 1, "oldest_started_at": 30.0},
        ],
        "truncated": True,
    }
    assert report["latest_executions"] == {
        "rows": [
            {
                "job_id": "job-c",
                "status": "running",
                "claimed_at": "2026-01-04T00:00:00Z",
                "finished_at": None,
                "has_error": False,
            },
            {
                "job_id": "job-b",
                "status": "failed",
                "claimed_at": "2026-01-03T00:00:00Z",
                "finished_at": "2026-01-03T00:01:00Z",
                "has_error": True,
            },
        ],
        "truncated": True,
    }
    assert "private detail" not in result.stdout
    for path, (contents, mtime_ns) in before.items():
        assert path.read_bytes() == contents
        assert path.stat().st_mtime_ns == mtime_ns
        assert not Path(str(path) + "-wal").exists()
        assert not Path(str(path) + "-shm").exists()

    nightly = NIGHTLY_SKILL.read_text(encoding="utf-8")
    assert "sqlite_health.py" in nightly
    assert 'read_file(path="$HERMES_HOME/state.db"' not in nightly
    assert 'read_file(path="$HERMES_HOME/cron/executions.db"' not in nightly


def test_sqlite_health_missing_databases_fail_closed_without_creation(tmp_path):
    state_db = tmp_path / "missing-state.db"
    executions_db = tmp_path / "missing-executions.db"
    result = subprocess.run(
        [
            sys.executable,
            str(SQLITE_HEALTH),
            "--state-db",
            str(state_db),
            "--executions-db",
            str(executions_db),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report == {"reason": "database unavailable", "status": "error"}
    assert not state_db.exists()
    assert not executions_db.exists()


class FakeResponse:
    def __init__(self, body: bytes, *, content_length: int | None = None):
        self.body = body
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1):
        return self.body if size < 0 else self.body[:size]


def test_upstream_semver_comparison_is_directional():
    upstream = load_script(UPSTREAM_CHECK, "upstream_semver")
    assert upstream.compare_versions("0.2.13", "0.2.14") == -1
    assert upstream.compare_versions("0.2.13", "0.2.13") == 0
    assert upstream.compare_versions("0.2.13", "0.2.12") == 1
    assert upstream.compare_versions("1.0.0-rc.1", "1.0.0") == -1
    with pytest.raises(ValueError):
        upstream.compare_versions("0.2", "0.2.13")


def test_upstream_fetch_is_bounded_before_decode():
    upstream = load_script(UPSTREAM_CHECK, "upstream_bounded")

    def opener(_request, timeout):
        assert timeout == upstream.TIMEOUT
        return FakeResponse(b"x" * 9)

    with pytest.raises(upstream.ResponseTooLarge):
        upstream.fetch_bytes("https" + "://example.invalid/data", opener=opener, max_bytes=8)


def test_upstream_context_is_bound_to_resolved_immutable_commit():
    upstream = load_script(UPSTREAM_CHECK, "upstream_provenance")
    commit = "a" * 40
    content = b"# immutable context\n"
    distribution = b"name: emh\nversion: 0.2.14\n"
    seen_urls = []

    def opener(request, timeout):
        assert timeout == upstream.TIMEOUT
        url = request.full_url
        seen_urls.append(url)
        if url == upstream.COMMIT_API:
            return FakeResponse(json.dumps({"sha": commit}).encode("ascii"))
        if url.endswith(f"/{commit}/distribution.yaml"):
            return FakeResponse(distribution)
        if url.endswith(f"/{commit}/skills/emh-triage/SKILL.md"):
            return FakeResponse(content)
        raise AssertionError(f"unexpected URL: {url}")

    report = upstream.probe(
        installed="0.2.13",
        fetch_path="skills/emh-triage/SKILL.md",
        opener=opener,
        max_bytes=4096,
    )

    assert report["status"] == "ok"
    assert report["upstream"] == "0.2.14"
    assert report["update_available"] is True
    assert report["content"] == content.decode("utf-8")
    assert report["provenance"] == {
        "commit": commit,
        "path": "skills/emh-triage/SKILL.md",
        "raw_url": upstream.raw_url(commit, "skills/emh-triage/SKILL.md"),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }
    assert all("/main/" not in url for url in seen_urls if "raw.githubusercontent" in url)


def test_new_diagnostic_scripts_are_executable():
    for path in (SQLITE_HEALTH, UPSTREAM_CHECK, BREAKGLASS):
        assert stat.S_IMODE(path.stat().st_mode) == 0o755
