from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

SERVICE_NAME = "codestra-natron"
ENGINE_NAME = "natron"
DEFAULT_ENGINE_BINARY = "NatronRenderer"
SUPPORTED_JOB_KINDS = ("render_project", "render_writer")


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.service_name = os.getenv("CODESTRA_SERVICE_NAME", SERVICE_NAME)
        self.engine_binary = os.getenv("CODESTRA_ENGINE_BINARY", DEFAULT_ENGINE_BINARY)
        self.db = Path(os.getenv("CODESTRA_DB_PATH", ".codestra-saas/jobs.sqlite3"))
        self.input_root = Path(os.getenv("CODESTRA_INPUT_ROOT", ".codestra-saas/inputs"))
        self.output_root = Path(os.getenv("CODESTRA_OUTPUT_ROOT", ".codestra-saas/outputs"))
        self.auth_mode = os.getenv("CODESTRA_AUTH_MODE", "required").strip().lower()
        self.api_key = os.getenv("CODESTRA_API_KEY")
        self.execution_enabled = _bool("CODESTRA_EXECUTION_ENABLED", False)
        self.timeout = max(1, int(os.getenv("CODESTRA_JOB_TIMEOUT_SECONDS", "3600")))
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.input_root.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)


class JobCreate(BaseModel):
    kind: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=128)
    parameters: dict[str, Any] = Field(default_factory=dict)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                project_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
                parameters_json TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                result_json TEXT, error TEXT, UNIQUE(tenant_id,idempotency_key))""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_tenant_status ON jobs(tenant_id,status,created_at)")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def as_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "tenant_id": row["tenant_id"], "actor_id": row["actor_id"],
            "project_id": row["project_id"], "kind": row["kind"], "status": row["status"],
            "parameters": json.loads(row["parameters_json"]), "idempotency_key": row["idempotency_key"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": row["error"],
        }

    def ping(self) -> None:
        with self.connect() as db:
            db.execute("SELECT 1").fetchone()

    def create(self, tenant: str, actor: str, payload: JobCreate, idem: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.lock, self.connect() as db:
            old = db.execute("SELECT * FROM jobs WHERE tenant_id=? AND idempotency_key=?", (tenant, idem)).fetchone()
            if old:
                return self.as_dict(old)
            job_id = f"job_{uuid4().hex}"
            db.execute("INSERT INTO jobs(id,tenant_id,actor_id,project_id,kind,status,parameters_json,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (job_id, tenant, actor, payload.project_id, payload.kind, "queued", json.dumps(payload.parameters, sort_keys=True), idem, now, now))
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return self.as_dict(row)

    def get(self, tenant: str, job_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE tenant_id=? AND id=?", (tenant, job_id)).fetchone()
            return self.as_dict(row) if row else None

    def list(self, tenant: str, limit: int) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?", (tenant, min(max(limit,1),100))).fetchall()
            return [self.as_dict(r) for r in rows]

    def cancel(self, tenant: str, job_id: str) -> dict[str, Any] | None:
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE tenant_id=? AND id=?", (tenant, job_id)).fetchone()
            if not row:
                return None
            if row["status"] in {"queued", "running"}:
                nxt = "cancelled" if row["status"] == "queued" else "cancel_requested"
                db.execute("UPDATE jobs SET status=?,updated_at=? WHERE id=?", (nxt, datetime.now(timezone.utc).isoformat(), job_id))
            return self.as_dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def claim(self) -> dict[str, Any] | None:
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                db.commit(); return None
            db.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=? AND status='queued'", (datetime.now(timezone.utc).isoformat(), row["id"]))
            db.commit()
            return self.as_dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def finish(self, job_id: str, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        with self.lock, self.connect() as db:
            db.execute("UPDATE jobs SET status=?,result_json=?,error=?,updated_at=? WHERE id=?",
                       ("failed" if error else "succeeded", json.dumps(result, sort_keys=True) if result else None, error, datetime.now(timezone.utc).isoformat(), job_id))


settings = Settings()
store = Store(settings.db)
app = FastAPI(title=f"{settings.service_name} Standalone SaaS API", version="1.0.0")


def auth(request: Request, authorization: str | None = Header(default=None)) -> None:
    cfg = request.app.state.settings
    if cfg.auth_mode == "development": return
    if cfg.auth_mode != "required" or not cfg.api_key: raise HTTPException(503, "service_auth_not_configured")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token, cfg.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_service_credential", headers={"WWW-Authenticate":"Bearer"})


def tenant(x_tenant_id: str = Header(alias="X-Tenant-ID")) -> str:
    value = x_tenant_id.strip()
    if not value or len(value) > 128: raise HTTPException(400, "invalid_tenant_id")
    return value


def actor(x_actor_id: str = Header(alias="X-Actor-ID")) -> str:
    value = x_actor_id.strip()
    if not value or len(value) > 128: raise HTTPException(400, "invalid_actor_id")
    return value


app.state.settings = settings
app.state.store = store


@app.get("/healthz")
def healthz() -> dict[str, str]: return {"status":"ok","service":settings.service_name}


@app.get("/readyz")
def readyz() -> dict[str, Any]:
    store.ping()
    if settings.auth_mode != "development" and not settings.api_key: raise HTTPException(503, "service_auth_not_configured")
    available = shutil.which(settings.engine_binary) is not None
    if settings.execution_enabled and not available: raise HTTPException(503, "engine_binary_unavailable")
    return {"status":"ready","database":"ok","engine_available":available,"execution_enabled":settings.execution_enabled}


@app.get("/v1/capabilities", dependencies=[Depends(auth)])
def capabilities() -> dict[str, Any]:
    return {"service":settings.service_name,"engine":ENGINE_NAME,"engine_binary":settings.engine_binary,
            "engine_available":shutil.which(settings.engine_binary) is not None,"execution_enabled":settings.execution_enabled,
            "supported_job_kinds":list(SUPPORTED_JOB_KINDS),"api_version":"v1"}


@app.post("/v1/jobs", status_code=202, dependencies=[Depends(auth)])
def create_job(payload: JobCreate, t: str = Depends(tenant), a: str = Depends(actor), idem: str = Header(alias="Idempotency-Key", min_length=8, max_length=128)) -> dict[str, Any]:
    if payload.kind not in SUPPORTED_JOB_KINDS: raise HTTPException(422, "unsupported_job_kind")
    return store.create(t, a, payload, idem)


@app.get("/v1/jobs", dependencies=[Depends(auth)])
def list_jobs(t: str = Depends(tenant), limit: int = Query(50, ge=1, le=100)) -> list[dict[str, Any]]: return store.list(t, limit)


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(auth)])
def get_job(job_id: str, t: str = Depends(tenant)) -> dict[str, Any]:
    job = store.get(t, job_id)
    if not job: raise HTTPException(404, "job_not_found")
    return job


@app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(auth)])
def cancel_job(job_id: str, t: str = Depends(tenant), _: str = Depends(actor), idem: str = Header(alias="Idempotency-Key", min_length=8, max_length=128)) -> dict[str, Any]:
    job = store.cancel(t, job_id)
    if not job: raise HTTPException(404, "job_not_found")
    return job


def bounded(root: Path, raw: str, must_exist: bool) -> Path:
    root = root.resolve(); path = Path(raw); path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    try: path.relative_to(root)
    except ValueError as exc: raise ValueError("path_outside_workspace") from exc
    if must_exist and not path.exists(): raise ValueError("input_not_found")
    return path


def command_for(job: dict[str, Any]) -> list[str]:
    p = job["parameters"]
    source = bounded(settings.input_root, str(p.get("source_path", "")), True)
    cmd = [settings.engine_binary, str(source)]
    if job["kind"] == "render_writer":
        writer = str(p.get("writer", "")).strip()
        if not writer or len(writer) > 128 or not all(ch.isalnum() or ch in "_.-" for ch in writer):
            raise ValueError("invalid_writer")
        cmd += ["-w", writer]
        if "output_path" in p:
            output = bounded(settings.output_root, str(p["output_path"]), False)
            cmd.append(str(output))
        if "first_frame" in p or "last_frame" in p:
            first = int(p.get("first_frame", p.get("last_frame", 1)))
            last = int(p.get("last_frame", first))
            if last < first:
                raise ValueError("invalid_frame_range")
            cmd.append(f"{first}-{last}")
    return cmd


def run_once() -> bool:
    job = store.claim()
    if not job: return False
    if not settings.execution_enabled: store.finish(job["id"], error="execution_disabled"); return True
    if shutil.which(settings.engine_binary) is None: store.finish(job["id"], error="engine_binary_unavailable"); return True
    try:
        done = subprocess.run(command_for(job), shell=False, capture_output=True, text=True, timeout=settings.timeout, check=False)
        result = {"returncode":done.returncode,"stdout_tail":done.stdout[-8000:],"stderr_tail":done.stderr[-8000:]}
        store.finish(job["id"], result=result, error=None if done.returncode == 0 else "engine_process_failed")
    except subprocess.TimeoutExpired: store.finish(job["id"], error="engine_process_timeout")
    except (ValueError, OSError) as exc: store.finish(job["id"], error=str(exc))
    return True


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--worker", action="store_true"); parser.add_argument("--once", action="store_true"); args = parser.parse_args()
    if not args.worker: parser.error("use uvicorn codestra_saas:app for API or --worker for worker")
    if args.once: run_once(); return
    while True:
        if not run_once(): time.sleep(1)


if __name__ == "__main__": main()
