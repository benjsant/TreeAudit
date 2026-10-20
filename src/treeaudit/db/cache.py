"""Cache of summaries for finished scans and analyses (they never change).

Keys include everything the summary depends on (scan or run id, version of the
rules or of the grouping logic), so a stale entry is never used: it is simply
not looked up anymore.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from treeaudit.db.scans import now_iso


def get(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT payload FROM summary_cache WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    payload: dict[str, Any] = json.loads(row[0])
    return payload


def put(conn: sqlite3.Connection, key: str, payload: dict[str, Any]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO summary_cache (key, payload, created_at) VALUES (?, ?, ?)",
        (key, json.dumps(payload), now_iso()),
    )
