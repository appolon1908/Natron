#!/usr/bin/env python3
"""Codestra natron local render sidecar.

Loopback-only by deployment contract. Middleware owns durable command state;
this service owns local process execution and readback only.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ENGINE = "natron"
SERVICE = "codestra-natron-api"
DEFAULT_BINARY = "NatronRenderer"
DEFAULT_PORT = 18103
ROOT = Path(os.environ.get("CODESTRA_VIDEO_ROOT", "/srv/codestra-video")).resolve()
LOG_DIR = ROOT / "logs" / ENGINE
TOKEN = os.environ.get("CODESTRA_VIDEO_API_TOKEN", "")
MAX_JOBS = max(1, int(os.environ.get("CODESTRA_VIDEO_MAX_JOBS", "1")))
RENDER_ENABLED = os.environ.get("CODESTRA_VIDEO_RENDER_ENABLED", "1").lower() in {"1", "true", "yes", "on"}
BINARY = os.environ.get("CODESTRA_VIDEO_BIN", DEFAULT_BINARY)
_NAME_RE = re.compile(r"^[A-Za-z0-9_.:+-]+$")

_lock = threading.RLock()
_jobs: dict[str, dict] = {}
_processes: dict[str, subprocess.Popen] = {}


def now() -> float:
    return time.time()


def safe_path(value: str, *, must_exist: bool = False) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path_required")
    raw = Path(value)
    candidate = (raw if raw.is_absolute() else ROOT / raw).resolve()
    try:
        candidate.relative_to(ROOT)
    except ValueError as exc:
        raise ValueError("path_outside_video_root") from exc
    if must_exist and not candidate.exists():
        raise ValueError("path_not_found")
    return candidate


def safe_name(value: str, field: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise ValueError(f"invalid_{field}")
    return value


def command_for(payload: dict) -> list[str]:
    if ENGINE == "blender":
        project = payload.get("project")
        script = payload.get("script")
        if not project and not script:
            raise ValueError("project_or_script_required")
        cmd = [BINARY, "-b"]
        if project:
            cmd.append(str(safe_path(project, must_exist=True)))
        if script:
            cmd += ["--python", str(safe_path(script, must_exist=True))]
        output = payload.get("output")
        if output:
            out = safe_path(output)
            out.parent.mkdir(parents=True, exist_ok=True)
            cmd += ["-o", str(out)]
        mode = payload.get("mode", "animation")
        if mode == "frame":
            frame = int(payload.get("frame", 1))
            cmd += ["-f", str(frame)]
        elif mode == "animation":
            if payload.get("frame_start") is not None:
                cmd += ["-s", str(int(payload["frame_start"]))]
            if payload.get("frame_end") is not None:
                cmd += ["-e", str(int(payload["frame_end"]))]
            cmd += ["-a"]
        else:
            raise ValueError("invalid_mode")
        return cmd

    if ENGINE == "mlt":
        project = safe_path(payload.get("project", ""), must_exist=True)
        output = safe_path(payload.get("output", ""))
        output.parent.mkdir(parents=True, exist_ok=True)
        cmd = [BINARY]
        profile = payload.get("profile")
        if profile:
            cmd += ["-profile", safe_name(profile, "profile")]
        cmd.append(str(project))
        cmd += ["-consumer", f"avformat:{output}"]
        mapping = {
            "vcodec": "vcodec",
            "acodec": "acodec",
            "format": "f",
            "preset": "preset",
            "crf": "crf",
        }
        for key, option in mapping.items():
            if payload.get(key) is not None:
                value = str(payload[key])
                if not _NAME_RE.fullmatch(value):
                    raise ValueError(f"invalid_{key}")
                cmd.append(f"{option}={value}")
        return cmd

    if ENGINE == "natron":
        project_value = payload.get("project")
        script_value = payload.get("script")
        if not project_value and not script_value:
            raise ValueError("project_or_script_required")
        cmd = [BINARY]
        if script_value and not project_value:
            cmd += ["--opengl", "disabled", "-t", str(safe_path(script_value, must_exist=True))]
            return cmd
        project = safe_path(project_value, must_exist=True)
        writer = payload.get("writer")
        output = payload.get("output")
        if writer:
            cmd += ["-w", safe_name(writer, "writer")]
            if output:
                out = safe_path(output)
                out.parent.mkdir(parents=True, exist_ok=True)
                cmd.append(str(out))
            start = payload.get("frame_start")
            end = payload.get("frame_end")
            if (start is None) ^ (end is None):
                raise ValueError("frame_range_requires_start_and_end")
            if start is not None:
                cmd.append(f"{int(start)}-{int(end)}")
        elif output:
            raise ValueError("output_requires_writer")
        onload = payload.get("onload")
        if onload:
            cmd += ["-l", str(safe_path(onload, must_exist=True))]
        cmd.append(str(project))
        return cmd

    raise RuntimeError("unknown_engine")


def public_job(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def running_count() -> int:
    with _lock:
        return sum(1 for j in _jobs.values() if j["state"] in {"queued", "running", "stopping"})


def launch(payload: dict) -> dict:
    if not RENDER_ENABLED:
        raise PermissionError("render_disabled")
    if shutil.which(BINARY) is None:
        raise FileNotFoundError(f"engine_binary_missing:{BINARY}")
    if running_count() >= MAX_JOBS:
        raise RuntimeError("job_capacity_reached")
    cmd = command_for(payload)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex
    log_path = LOG_DIR / f"{job_id}.log"
    job = {
        "job_id": job_id,
        "engine": ENGINE,
        "state": "queued",
        "created_at": now(),
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "pid": None,
        "log": str(log_path),
    }
    with _lock:
        _jobs[job_id] = job

    def worker() -> None:
        try:
            with log_path.open("ab", buffering=0) as log:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                with _lock:
                    _processes[job_id] = proc
                    _jobs[job_id].update(state="running", pid=proc.pid, started_at=now())
                rc = proc.wait()
                with _lock:
                    state = "succeeded" if rc == 0 else ("stopped" if _jobs[job_id]["state"] == "stopping" else "failed")
                    _jobs[job_id].update(state=state, exit_code=rc, finished_at=now())
                    _processes.pop(job_id, None)
        except Exception as exc:
            with _lock:
                _jobs[job_id].update(state="failed", error=str(exc), finished_at=now())
                _processes.pop(job_id, None)

    threading.Thread(target=worker, name=f"{ENGINE}-{job_id}", daemon=True).start()
    return dict(job)


def stop(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        proc = _processes.get(job_id)
        if not job:
            raise KeyError("job_not_found")
        if not proc or proc.poll() is not None:
            return dict(job)
        job["state"] = "stopping"
        proc.terminate()

    def force_kill() -> None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    threading.Thread(target=force_kill, daemon=True).start()
    return public_job(job_id) or {}


class Handler(BaseHTTPRequestHandler):
    server_version = f"CodestraVideo/{ENGINE}"

    def log_message(self, fmt: str, *args) -> None:
        return

    def auth_ok(self) -> bool:
        if not TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def send_json(self, status: int, body: dict) -> None:
        raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 1024 * 1024:
            raise ValueError("payload_too_large")
        raw = self.rfile.read(size)
        if not raw:
            return {}
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("json_object_required")
        return value

    def do_GET(self) -> None:
        if not self.auth_ok():
            self.send_json(401, {"error": "unauthorized"})
            return
        path = urlparse(self.path).path
        if path == "/health":
            self.send_json(200, {
                "status": "ok",
                "service": SERVICE,
                "engine": ENGINE,
                "binary": BINARY,
                "engine_available": shutil.which(BINARY) is not None,
                "render_enabled": RENDER_ENABLED,
                "max_jobs": MAX_JOBS,
            })
            return
        if path == "/status":
            with _lock:
                jobs = [dict(v) for v in _jobs.values()]
            self.send_json(200, {"service": SERVICE, "running": running_count(), "jobs": jobs[-50:]})
            return
        prefix = "/status/"
        if path.startswith(prefix):
            job = public_job(path[len(prefix):])
            self.send_json(200 if job else 404, job or {"error": "job_not_found"})
            return
        self.send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self.auth_ok():
            self.send_json(401, {"error": "unauthorized"})
            return
        path = urlparse(self.path).path
        try:
            if path == "/render":
                job = launch(self.read_json())
                self.send_json(202, job)
                return
            prefix = "/stop/"
            if path.startswith(prefix):
                self.send_json(202, stop(path[len(prefix):]))
                return
            self.send_json(404, {"error": "not_found"})
        except PermissionError as exc:
            self.send_json(403, {"error": str(exc)})
        except FileNotFoundError as exc:
            self.send_json(503, {"error": str(exc)})
        except KeyError as exc:
            self.send_json(404, {"error": str(exc).strip("'")})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except RuntimeError as exc:
            self.send_json(409, {"error": str(exc)})
        except Exception:
            self.send_json(500, {"error": "internal_error"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    if args.self_test:
        assert safe_path("outputs/test.dat") == ROOT / "outputs/test.dat"
        try:
            safe_path("/etc/passwd")
        except ValueError:
            pass
        else:
            raise AssertionError("path guard failed")
        print(json.dumps({"ok": True, "engine": ENGINE, "service": SERVICE}))
        return
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("refusing_non_loopback_bind")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
