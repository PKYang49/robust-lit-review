"""Small durable job store; pipeline evidence remains in its original files."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

ACTIVE = {"drafting", "searching", "running"}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "jobs.sqlite3"
        with self.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db, timeout=15)

    def base(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", job_id):
            raise ValueError("無效的查詢識別碼。")
        path = (self.root / job_id).resolve()
        if path.parent != self.root:
            raise ValueError("無效的查詢路徑。")
        return path

    def get(self, job_id: str) -> dict[str, Any]:
        self.base(job_id)
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return json.loads(row[0])

    def list(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT data FROM jobs").fetchall()
        return sorted((json.loads(row[0]) for row in rows), key=lambda j: j["updated_at"], reverse=True)

    def create(self, question: str) -> dict[str, Any]:
        job_id = uuid4().hex
        self.base(job_id).mkdir(mode=0o700)
        job = {"id": job_id, "question": question, "status": "drafting",
               "message": "正在整理 PICO 問題…", "updated_at": now(), "events": [], "picos": [],
               "preview": [], "studies": {}, "gaps": {}, "report_url": None, "phase": "draft"}
        with self.connect() as conn:
            conn.execute("INSERT INTO jobs VALUES (?, ?)", (job_id, json.dumps(job, ensure_ascii=False)))
        return job

    def import_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Create a local working copy of an authenticated cloud request."""
        self.base(job["id"]).mkdir(mode=0o700, exist_ok=True)
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO jobs VALUES (?, ?)",
                         (job["id"], json.dumps(job, ensure_ascii=False)))
        return self.get(job["id"])

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            job = json.loads(row[0])
            job.update(changes, updated_at=now())
            if "message" in changes:
                job["events"] = (job.get("events", []) + [{"at": now(), "message": changes["message"]}])[-60:]
            conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job_id))
        return job

    def recover(self) -> None:
        for job in self.list():
            if job["status"] in ACTIVE:
                self.update(job["id"], status="interrupted", message="服務曾重新啟動，已保存進度。請按繼續。")


def write_json(path: Path, data: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
