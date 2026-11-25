"""Local history of screened deals (one SQLite file, nothing to set up)."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.getenv("SCREENING_DB") or Path(__file__).resolve().parent.parent / "data" / "screening.db")

# Summary columns for the history table, read straight from the stored JSON (no schema migration needed).
_LIST = """SELECT id, created, company, overall, decision, fit,
  json_extract(result, '$.version') AS version, json_extract(result, '$.website') AS website,
  json_extract(result, '$.context.stage') AS stage, json_extract(result, '$.context.model_label') AS model,
  json_extract(result, '$.confidence.level') AS confidence, json_extract(result, '$.thesis.id') AS thesis_id,
  COALESCE(json_extract(result, '$.thesis.name'), json_extract(result, '$.fund')) AS thesis_name, json_extract(result, '$.claim_counts.verified') AS verified,
  json_array_length(result, '$.claims') - COALESCE(json_extract(result, '$.claim_counts.not_checked'), 0) AS claims, json_extract(result, '$.red_flag_count') AS red_flags
  FROM runs ORDER BY id DESC LIMIT ?"""


class Store:
    def __init__(self, path: Path = DB_PATH):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, company TEXT, source TEXT,
                overall REAL, decision TEXT, fit TEXT, result TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS theses (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE, data TEXT NOT NULL,
                created TEXT NOT NULL, updated TEXT NOT NULL)""")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def save(self, result: dict) -> int:
        with self._db() as db:
            cur = db.execute(
                "INSERT INTO runs (created, company, source, overall, decision, fit, result) VALUES (?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), result["company"],
                 result.get("website") or result.get("source", ""), result["overall"], result["decision"],
                 result["fit"]["status"], json.dumps(result, ensure_ascii=False)))
            return int(cur.lastrowid)

    def list(self, limit: int = 300) -> list[dict]:
        with self._db() as db:
            return [dict(r) for r in db.execute(_LIST, (limit,)).fetchall()]

    # ---------- fund theses ----------
    def list_theses(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("""SELECT t.id, t.name, t.data, t.updated,
                (SELECT COUNT(*) FROM runs r WHERE json_extract(r.result, '$.thesis.id') = t.id
                   OR (json_extract(r.result, '$.thesis.id') IS NULL AND json_extract(r.result, '$.fund') = t.name)) AS runs
                FROM theses t ORDER BY t.updated DESC, t.id DESC""").fetchall()
        return [{"id": r["id"], "name": r["name"], "data": json.loads(r["data"]), "updated": r["updated"], "runs": r["runs"]} for r in rows]

    def get_thesis(self, thesis_id: int) -> dict | None:
        with self._db() as db:
            r = db.execute("""SELECT id, name, data, updated,
                (SELECT COUNT(*) FROM runs WHERE json_extract(result, '$.thesis.id') = theses.id
                   OR (json_extract(result, '$.thesis.id') IS NULL AND json_extract(result, '$.fund') = theses.name)) AS runs
                FROM theses WHERE id = ?""", (thesis_id,)).fetchone()
        return {"id": r["id"], "name": r["name"], "data": json.loads(r["data"]), "updated": r["updated"], "runs": r["runs"]} if r else None

    def save_thesis(self, data: dict, thesis_id: int | None = None) -> int:
        """Insert or update. Names are unique (case-insensitive); raises ValueError with a readable message otherwise."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with self._db() as db:
                if thesis_id is None:
                    return int(db.execute("INSERT INTO theses (name, data, created, updated) VALUES (?,?,?,?)",
                                          (data["fund_name"], json.dumps(data, ensure_ascii=False), now, now)).lastrowid)
                if not db.execute("UPDATE theses SET name=?, data=?, updated=? WHERE id=?",
                                  (data["fund_name"], json.dumps(data, ensure_ascii=False), now, thesis_id)).rowcount:
                    raise LookupError("That fund thesis no longer exists.")
                return thesis_id
        except sqlite3.IntegrityError:
            raise ValueError(f"A fund thesis named '{data['fund_name']}' already exists. Choose another name.") from None

    def delete_thesis(self, thesis_id: int) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM theses WHERE id = ?", (thesis_id,)).rowcount > 0

    def delete(self, run_id: int) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM runs WHERE id = ?", (run_id,)).rowcount > 0

    def get(self, run_id: int) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT result, created FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        return {"created": row["created"], **json.loads(row["result"])}
