"""SQLite store for batch runs and what-if results (milestone M6).

Schema changes are forward-only migrations (see cards/standing-defaults.md):
append a statement to ``MIGRATIONS``; never edit one that has shipped.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

MIGRATIONS = [
    """CREATE TABLE batch (
        id INTEGER PRIMARY KEY,
        created REAL NOT NULL,
        kind TEXT NOT NULL,              -- 'batch' or 'whatif'
        model_name TEXT NOT NULL,
        model_json TEXT NOT NULL,
        params_json TEXT NOT NULL,       -- scenarios, seeds, until / from, horizon
        status TEXT NOT NULL,            -- running, done, failed
        error TEXT
    )""",
    """CREATE TABLE run (
        id INTEGER PRIMARY KEY,
        batch_id INTEGER NOT NULL REFERENCES batch(id) ON DELETE CASCADE,
        scenario TEXT NOT NULL,
        seed INTEGER,
        events INTEGER NOT NULL,
        wall_ms REAL NOT NULL,
        truncated INTEGER NOT NULL,
        flat_json TEXT NOT NULL,         -- {metric path: number} at the end
        series_json TEXT NOT NULL        -- [[t, {metric path: number}], ...]
    )""",
    "CREATE INDEX run_batch ON run(batch_id)",
]


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > len(MIGRATIONS):
        raise RuntimeError(
            f"database schema is version {version}, but this code knows only "
            f"{len(MIGRATIONS)}: it was written by newer code"
        )
    for i, sql in enumerate(MIGRATIONS[version:], start=version):
        try:
            conn.executescript(f"BEGIN; {sql}; PRAGMA user_version = {i + 1}; COMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise


class RunStore:
    """One SQLite file; a short-lived connection per call (safe across threads)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        with closing(self._connect()) as conn:
            migrate(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")  # outside any transaction
        return conn

    def create_batch(self, kind: str, model: dict[str, Any], params: dict[str, Any]) -> int:
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO batch (created, kind, model_name, model_json, params_json, status)"
                " VALUES (?, ?, ?, ?, ?, 'running')",
                (
                    time.time(),
                    kind,
                    model.get("name", "untitled"),
                    json.dumps(model),
                    json.dumps(params),
                ),
            )
            return int(cur.lastrowid)

    def add_run(
        self, batch_id: int, scenario: str, seed: int | None, result: dict[str, Any]
    ) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO run (batch_id, scenario, seed, events, wall_ms, truncated,"
                " flat_json, series_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    scenario,
                    seed,
                    result["events"],
                    result["wall_ms"],
                    int(result["truncated"]),
                    json.dumps(result["flat"]),
                    json.dumps(result["series"]),
                ),
            )

    def finish(self, batch_id: int, error: str | None = None) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE batch SET status = ?, error = ? WHERE id = ?",
                ("failed" if error else "done", error, batch_id),
            )

    def batches(self, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT b.id, b.created, b.kind, b.model_name, b.params_json, b.status, b.error,"
                " COUNT(r.id) AS runs FROM batch b LEFT JOIN run r ON r.batch_id = b.id"
                " GROUP BY b.id ORDER BY b.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_batch_row(r) for r in rows]

    def batch(self, batch_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            b = conn.execute(
                "SELECT b.*, (SELECT COUNT(*) FROM run WHERE batch_id = b.id) AS runs"
                " FROM batch b WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if b is None:
                return None
            runs = conn.execute(
                "SELECT scenario, seed, events, wall_ms, truncated, flat_json, series_json"
                " FROM run WHERE batch_id = ? ORDER BY id",
                (batch_id,),
            ).fetchall()
        out = _batch_row(b)
        out["runs_detail"] = [
            {
                "scenario": r["scenario"],
                "seed": r["seed"],
                "events": r["events"],
                "wall_ms": r["wall_ms"],
                "truncated": bool(r["truncated"]),
                "flat": json.loads(r["flat_json"]),
                "series": json.loads(r["series_json"]),
            }
            for r in runs
        ]
        return out


def _batch_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "created": r["created"],
        "kind": r["kind"],
        "model_name": r["model_name"],
        "params": json.loads(r["params_json"]),
        "status": r["status"],
        "error": r["error"],
        "runs": r["runs"],
    }
