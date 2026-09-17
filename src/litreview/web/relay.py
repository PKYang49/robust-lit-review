"""Poll Cloudflare for queued commands and publish local pipeline progress."""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from litreview.pipeline import brief, brief_flow
from litreview.web.app import REPO
from litreview.web.runner import BriefWorker, ClaudeRunner, PicoDraft, WorkError
from litreview.web.store import ACTIVE, Store, write_json

logger = logging.getLogger(__name__)


class Relay:
    def __init__(self, url: str, token: str, client_id: str, client_secret: str):
        if not url.startswith("https://"):
            raise ValueError("Cloudflare relay requires an HTTPS URL")
        self.client = httpx.Client(base_url=url.rstrip("/"), timeout=30,
                                   headers={"X-Evidence-Sync-Token": token,
                                            "CF-Access-Client-Id": client_id,
                                            "CF-Access-Client-Secret": client_secret})

    def post(self, route: str, data: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(f"/api/worker/{route}", json=data)
        if response.status_code == 409:
            raise WorkError("雲端工作租約已更新；本次結果保留在本機。")
        if not response.is_success or "application/json" not in response.headers.get("content-type", ""):
            raise WorkError(f"雲端同步失敗（HTTP {response.status_code}），請檢查 Access 與同步設定。")
        return response.json()

    def close(self) -> None:
        self.client.close()


def prepare(store: Store, job: dict[str, Any], command: dict[str, Any]) -> dict[str, Any]:
    """Apply each cloud command once, including after a lease is reclaimed."""
    job_id = job["id"]
    local = store.import_job(dict(job, phase="draft"))
    command_id = command["id"]
    if local.get("cloud_command_id") == command_id:
        return local
    kind, payload = command["kind"], command["payload"]
    base = store.base(job_id)
    changes: dict[str, Any] = {"cloud_command_id": command_id, "status": "running"}
    if kind == "draft":
        changes.update(phase="draft", status="drafting", message="Mac 已接收問題，正在整理 PICO…")
    elif kind == "approve":
        draft = PicoDraft.model_validate(payload)
        data = {"question": job["question"], **draft.model_dump()}
        write_json(base / "picos.json", data)
        changes.update(picos=data["picos"], min_year=data["min_year"], phase="preview",
                       status="searching", preview=[],
                       message="Mac 已接收，正在檢查 PubMed 搜尋詞…")
    elif kind == "edit_pico":
        instruction = payload.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 2000:
            raise WorkError("PICO 修改指示格式不正確，請重新輸入。")
        changes.update(phase="draft", status="drafting", edit_instruction=instruction.strip(),
                       message="Mac 已接收修改指示，正在重新整理 PICO…")
    elif kind == "checkpoint":
        if not (base / "picos.json").exists():
            raise WorkError("本機缺少這筆查詢的研究資料，請從原處理主機繼續。")
        changes.update(phase="checkpoint", checkpoint_payload=payload, message="正在繼續評讀…")
    elif kind == "auto_checkpoint":
        if not (base / "picos.json").exists():
            raise WorkError("本機缺少這筆查詢的研究資料，無法自動核對。")
        changes.update(phase="auto_checkpoint", message="專家核對已等待 5 分鐘，Opus 正在自動核對…")
    elif kind == "resume":
        if not local.get("phase") or (local["phase"] != "draft" and not (base / "picos.json").exists()):
            raise WorkError("本機缺少這筆查詢的進度，請從原處理主機繼續。")
        changes.update(message="已接續上次進度…")
    else:
        raise WorkError("不支援的雲端指令。")
    return store.update(job_id, **changes)


def snapshot(store: Store, job_id: str) -> dict[str, Any]:
    allowed = {"id", "question", "status", "message", "updated_at", "picos", "preview", "studies", "gaps",
               "states", "events", "report_url", "fulltext", "additions_result", "min_year"}
    return {k: v for k, v in store.get(job_id).items() if k in allowed}


def process_claim(relay: Relay, worker: BriefWorker, claimed: dict[str, Any]) -> None:
    job, command = claimed["job"], claimed["command"]
    job_id, lease_token = job["id"], claimed["lease_token"]
    store = worker.store

    def sync() -> dict[str, Any]:
        return relay.post("update", {"id": job_id, "lease_token": lease_token,
                                      "brief": snapshot(store, job_id)})

    try:
        local = prepare(store, job, command)
    except Exception as exc:
        logger.exception("Cloud command preparation failed for %s", job_id)
        store.import_job(dict(job, phase="draft"))
        message = str(exc) if isinstance(exc, WorkError) else "查詢資料格式不完整，請重新送出。"
        local = store.update(job_id, status="error", message=message)
    if local["status"] in ACTIVE:
        sync()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(worker.execute, job_id)
            while not future.done():
                try:
                    future.result(timeout=10)
                except concurrent.futures.TimeoutError:
                    # A failed sync never marks work as uploaded; the same command
                    # can be reclaimed and the local snapshot reused after reconnect.
                    try:
                        sync()
                    except (httpx.HTTPError, WorkError):
                        logger.warning("Progress upload delayed for %s", job_id)
            future.result()
    final = snapshot(store, job_id)
    payload: dict[str, Any] = {"id": job_id, "lease_token": lease_token, "brief": final, "finished": True}
    if final["status"] == "done":
        report = store.base(job_id) / "brief.html"
        payload["report"] = report.read_text(encoding="utf-8")
    relay.post("update", payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evidence Brief Cloudflare background worker")
    parser.add_argument("--once", action="store_true", help="Process one pending command, then exit")
    parser.add_argument("--interval", type=int, default=20)
    args = parser.parse_args()
    root = Path(os.environ.get("EVIDENCE_BRIEF_DATA", str(REPO / ".evidence-brief"))).expanduser().resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    load_dotenv(root / "cloud.env", override=False)
    # Reuse the existing Weekly Journal credentials when this machine already
    # has the JournalFetcher worker configured. The relay URL remains local to
    # Evidence Brief, while the credential values are read without copying
    # secrets into this repository.
    journal_env = Path(os.environ.get("JOURNALFETCHER_ENV", "/Users/pokai/JournalFetcher/.env"))
    load_dotenv(journal_env, override=False)
    if not os.environ.get("EVIDENCE_BRIEF_SYNC_TOKEN"):
        legacy_token = os.environ.get("FEEDBACK_SYNC_TOKEN")
        if legacy_token:
            os.environ["EVIDENCE_BRIEF_SYNC_TOKEN"] = legacy_token
    required = ["EVIDENCE_BRIEF_RELAY", "EVIDENCE_BRIEF_SYNC_TOKEN", "CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET"]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Missing cloud settings: " + ", ".join(missing))
    logging.basicConfig(level=logging.INFO, filename=root / "relay.log",
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # launchd and a manual invocation must never consume the same local workspace.
    with (root / "relay.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Evidence Brief worker is already running") from None
        store = Store(root / "cloud-jobs")
        cfg = brief.brief_config()
        worker = BriefWorker(store, cfg, ClaudeRunner())
        relay = Relay(*(os.environ[name] for name in required))
        fulltext = brief_flow.fulltext_available(cfg)
        try:
            while True:
                try:
                    relay.post("heartbeat", {"fulltext": fulltext})
                    claimed = relay.post("claim", {"worker_id": socket.gethostname()})
                    if claimed.get("job"):
                        process_claim(relay, worker, claimed)
                except (httpx.HTTPError, WorkError):
                    logger.exception("Cloud synchronization will retry")
                    if args.once:
                        raise
                if args.once:
                    break
                time.sleep(max(5, args.interval))
        finally:
            relay.close()


if __name__ == "__main__":
    main()
