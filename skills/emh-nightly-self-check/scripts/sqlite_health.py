#!/usr/bin/env python3
"""Bounded read-only SQLite facts for the EMH nightly self-check."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path


DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _connect_read_only(path):
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(str(target))
    uri = target.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _bounded_rows(connection, query, limit):
    rows = connection.execute(query, (limit + 1,)).fetchall()
    return rows[:limit], len(rows) > limit


def inspect_databases(state_db, executions_db, limit=DEFAULT_LIMIT):
    limit = int(limit)
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError("limit out of range")

    with _connect_read_only(state_db) as state_connection:
        session_rows, sessions_truncated = _bounded_rows(
            state_connection,
            """SELECT source, COUNT(*) AS open_count,
                      MIN(started_at) AS oldest_started_at
                 FROM sessions
                WHERE ended_at IS NULL
                GROUP BY source
                ORDER BY open_count DESC, source ASC
                LIMIT ?""",
            limit,
        )

    with _connect_read_only(executions_db) as executions_connection:
        execution_rows, executions_truncated = _bounded_rows(
            executions_connection,
            """SELECT current.job_id, current.status, current.claimed_at,
                      current.finished_at,
                      CASE WHEN COALESCE(current.error, '') = '' THEN 0 ELSE 1 END
                        AS has_error
                 FROM executions AS current
                WHERE NOT EXISTS (
                      SELECT 1
                        FROM executions AS newer
                       WHERE newer.job_id = current.job_id
                         AND (newer.claimed_at > current.claimed_at
                              OR (newer.claimed_at = current.claimed_at
                                  AND newer.id > current.id)))
                ORDER BY current.claimed_at DESC, current.id DESC
                LIMIT ?""",
            limit,
        )

    return {
        "status": "ok",
        "limit": limit,
        "open_sessions": {
            "rows": [dict(row) for row in session_rows],
            "truncated": sessions_truncated,
        },
        "latest_executions": {
            "rows": [
                {
                    "job_id": row["job_id"],
                    "status": row["status"],
                    "claimed_at": row["claimed_at"],
                    "finished_at": row["finished_at"],
                    "has_error": bool(row["has_error"]),
                }
                for row in execution_rows
            ],
            "truncated": executions_truncated,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--executions-db", required=True)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()

    try:
        report = inspect_databases(args.state_db, args.executions_db, args.limit)
    except FileNotFoundError:
        print(json.dumps({"reason": "database unavailable", "status": "error"}, sort_keys=True))
        return 1
    except (sqlite3.Error, ValueError):
        print(json.dumps({"reason": "read-only query failed", "status": "error"}, sort_keys=True))
        return 1

    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
