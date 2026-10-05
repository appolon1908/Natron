from pathlib import Path
import importlib

from fastapi.testclient import TestClient


def load(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODESTRA_AUTH_MODE", "development")
    monkeypatch.setenv("CODESTRA_DB_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setenv("CODESTRA_INPUT_ROOT", str(tmp_path / "inputs"))
    monkeypatch.setenv("CODESTRA_OUTPUT_ROOT", str(tmp_path / "outputs"))
    import codestra_saas
    return importlib.reload(codestra_saas)


def test_health_ready_and_idempotency(tmp_path, monkeypatch):
    mod = load(tmp_path, monkeypatch); client = TestClient(mod.app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    h={"X-Tenant-ID":"t1","X-Actor-ID":"a1","Idempotency-Key":"idem-0001"}
    p={"kind":"render_project","project_id":"p1","parameters":{}}
    one=client.post("/v1/jobs",headers=h,json=p); two=client.post("/v1/jobs",headers=h,json=p)
    assert one.status_code == 202 and one.json()["id"] == two.json()["id"]
    assert client.get(f"/v1/jobs/{one.json()['id']}",headers={"X-Tenant-ID":"other"}).status_code == 404


def test_natron_writer_command_is_bounded(tmp_path, monkeypatch):
    mod = load(tmp_path, monkeypatch)
    src=mod.settings.input_root / "project.ntp"; src.parent.mkdir(parents=True,exist_ok=True); src.write_text("project")
    job={"kind":"render_writer","parameters":{"source_path":"project.ntp","writer":"Writer1","output_path":"rendered###.exr","first_frame":10,"last_frame":40}}
    cmd=mod.command_for(job)
    assert cmd[-4:] == ["-w","Writer1",str((mod.settings.output_root / "rendered###.exr").resolve()),"10-40"]
    bad={"kind":"render_writer","parameters":{"source_path":"../escape.ntp","writer":"Writer1"}}
    try: mod.command_for(bad)
    except ValueError as exc: assert str(exc) == "path_outside_workspace"
    else: raise AssertionError("path traversal accepted")
