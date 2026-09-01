#!/usr/bin/env python3
"""EMH break-glass collector — stdlib only, no Hermes imports.

Runs on a machine where Hermes may be broken or absent. Emits a redacted
machine baseline as JSON (stdout or --out FILE). Never reads credentials,
memories, raw logs, private paths, or configuration secrets.

Usage:
  python3 breakglass_collect.py --out baseline.json
"""

import argparse
import datetime
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path


def _run(command):
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=15
        )
        return (result.stdout or result.stderr).strip() or None
    except Exception:
        return None


def _hermes_version(output):
    match = re.search(r"\bHermes Agent v([0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?)\b", output or "")
    return match.group(1) if match else None


def _profile_count(output):
    count = 0
    for line in (output or "").splitlines():
        candidate = line.strip().lstrip("* ")
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", candidate):
            count += 1
    return count


def _home_kind():
    configured = os.environ.get("HERMES_HOME")
    if not configured:
        return "default"
    default = Path.home() / ".hermes"
    try:
        return "default" if Path(configured).expanduser().resolve() == default.resolve() else "custom"
    except OSError:
        return "custom"


def _disk_summary():
    probe = Path(os.environ.get("HERMES_HOME") or Path.home())
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return None
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }


def collect_baseline():
    """Return useful machine classes and counts without emitting private values."""
    hermes_path = shutil.which("hermes")
    version_output = _run(["hermes", "--version"]) if hermes_path else None
    profiles_output = _run(["hermes", "profile", "list"]) if hermes_path else None
    return {
        "collected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "disk": _disk_summary(),
        "hermes": {
            "on_path": hermes_path is not None,
            "version": _hermes_version(version_output),
            "home_kind": _home_kind(),
            "profile_count": _profile_count(profiles_output),
        },
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=None, help="write baseline JSON to FILE")
    args = ap.parse_args()

    baseline = collect_baseline()

    payload = json.dumps(baseline, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(payload)
        print("baseline written")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
