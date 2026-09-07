#!/usr/bin/env python3
"""EMH upstream knowledge probe — stdlib only, read-only.

Compares the installed EMH distribution version against a resolved immutable
GitHub commit, and optionally fetches one repository file as bounded JSON with
commit, URL, digest, and size provenance. Never installs, never updates, never
writes anything. Fail-closed: network and provenance errors are a diagnostic result.

Usage:
  python3 upstream_check.py --installed 0.2.5
  python3 upstream_check.py --fetch skills/emh-gateway-diagnostics/SKILL.md
  python3 upstream_check.py --commit <reviewed-40-character-sha> --installed 0.2.5
  python3 upstream_check.py --offline --installed 0.2.5
"""

import argparse
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request

REPOSITORY = "AtlasOmnia/EMH-A-Hermes-Diagnostic-Profile"
COMMIT_API = "https://api.github.com/repos/AtlasOmnia/EMH-A-Hermes-Diagnostic-Profile/commits/main"
RAW_TEMPLATE = "https://raw.githubusercontent.com/AtlasOmnia/EMH-A-Hermes-Diagnostic-Profile/{commit}/{path}"
DISTRIBUTION = "distribution.yaml"
TIMEOUT = 15
MAX_BYTES = 256 * 1024
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SEMVER_RE = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


class ResponseTooLarge(ValueError):
    pass


def _opener(opener):
    return opener if opener is not None else urllib.request.urlopen


def fetch_bytes(url, *, opener=None, max_bytes=MAX_BYTES):
    if max_bytes < 1 or max_bytes > MAX_BYTES:
        raise ValueError("invalid response bound")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "emh-upstream-probe",
        },
    )
    with _opener(opener)(request, timeout=TIMEOUT) as response:
        declared = response.headers.get("Content-Length")
        if declared is not None and int(declared) > max_bytes:
            raise ResponseTooLarge("response exceeds byte limit")
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ResponseTooLarge("response exceeds byte limit")
    return body


def _decode(data):
    return data.decode("utf-8")


def _semver(value):
    match = _SEMVER_RE.fullmatch(value)
    if not match:
        raise ValueError("invalid semantic version")
    core = tuple(int(item) for item in match.group(1, 2, 3))
    prerelease = match.group(4)
    identifiers = None if prerelease is None else tuple(prerelease.split("."))
    if identifiers is not None:
        for identifier in identifiers:
            if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
                raise ValueError("invalid semantic version")
    return core, identifiers


def _compare_prerelease(left, right):
    if left is None:
        return 0 if right is None else 1
    if right is None:
        return -1
    for left_item, right_item in zip(left, right):
        if left_item == right_item:
            continue
        left_numeric = left_item.isdigit()
        right_numeric = right_item.isdigit()
        if left_numeric and right_numeric:
            return -1 if int(left_item) < int(right_item) else 1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return -1 if left_item < right_item else 1
    return (len(left) > len(right)) - (len(left) < len(right))


def compare_versions(installed, upstream):
    """Return -1/0/1 when installed is older/equal/newer than upstream."""
    installed_core, installed_pre = _semver(installed)
    upstream_core, upstream_pre = _semver(upstream)
    if installed_core != upstream_core:
        return (installed_core > upstream_core) - (installed_core < upstream_core)
    return _compare_prerelease(installed_pre, upstream_pre)


def _valid_path(path):
    if not path or path.startswith("/") or ".." in path.split("/"):
        raise ValueError("invalid path")
    if any(part in {"", "."} for part in path.split("/")):
        raise ValueError("invalid path")
    return path


def raw_url(commit, path):
    if not _COMMIT_RE.fullmatch(commit):
        raise ValueError("invalid commit")
    return RAW_TEMPLATE.format(
        commit=commit,
        path=urllib.parse.quote(_valid_path(path), safe="/"),
    )


def resolve_commit(*, opener=None, max_bytes=MAX_BYTES):
    payload = json.loads(_decode(fetch_bytes(COMMIT_API, opener=opener, max_bytes=max_bytes)))
    commit = payload.get("sha") if isinstance(payload, dict) else None
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        raise ValueError("invalid commit response")
    return commit


def _version_from_distribution(text):
    versions = []
    for line in text.splitlines():
        match = re.fullmatch(r"\s*version:\s*([^\s#]+)\s*(?:#.*)?", line)
        if match:
            versions.append(match.group(1))
    if len(versions) != 1:
        raise ValueError("invalid distribution version")
    _semver(versions[0])
    return versions[0]


def _provenance(commit, path, content):
    return {
        "commit": commit,
        "path": path,
        "raw_url": raw_url(commit, path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def probe(*, installed="", fetch_path="", commit="", opener=None, max_bytes=MAX_BYTES):
    commit = commit or resolve_commit(opener=opener, max_bytes=max_bytes)
    if not _COMMIT_RE.fullmatch(commit):
        raise ValueError("invalid commit")
    distribution = fetch_bytes(
        raw_url(commit, DISTRIBUTION), opener=opener, max_bytes=max_bytes
    )
    upstream = _version_from_distribution(_decode(distribution))
    report = {
        "status": "ok",
        "installed": installed or "unknown",
        "upstream": upstream,
        "update_available": bool(installed) and compare_versions(installed, upstream) < 0,
        "provenance": _provenance(commit, DISTRIBUTION, distribution),
    }
    if fetch_path:
        path = _valid_path(fetch_path)
        content = fetch_bytes(raw_url(commit, path), opener=opener, max_bytes=max_bytes)
        report["content"] = _decode(content)
        report["provenance"] = _provenance(commit, path, content)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", default="", help="installed distribution version")
    parser.add_argument("--fetch", default="", help="repo-relative path to fetch (read-only)")
    parser.add_argument("--commit", default="", help="optional reviewed 40-character commit")
    parser.add_argument("--offline", action="store_true", help="never touch the network")
    args = parser.parse_args()

    if args.offline:
        print(json.dumps({"status": "unavailable", "reason": "offline mode"}))
        return 0

    try:
        print(json.dumps(probe(
            installed=args.installed,
            fetch_path=args.fetch,
            commit=args.commit,
        ), sort_keys=True))
    except Exception as exc:  # network failures are a diagnostic result, not a crash
        print(json.dumps({"status": "error", "reason": type(exc).__name__}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
