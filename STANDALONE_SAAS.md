# Natron Standalone SaaS Baseline

This additive sidecar makes Natron independently callable without requiring Codestra Middleware, Kong, or Caddy. Natron remains the native compositing/VFX engine; the sidecar owns the API, tenant/job boundary, durable local ledger, and safe worker invocation.

Implemented now:
- FastAPI + OpenAPI (`/openapi.json`, `/docs`)
- `/healthz`, `/readyz`, `/v1/capabilities`
- tenant-scoped durable SQLite job ledger
- `Idempotency-Key` on job create/cancel
- bearer service authentication that fails closed outside development mode
- `render_project` and `render_writer` jobs
- bounded input/output workspaces and `shell=False` NatronRenderer execution
- optional writer/output/frame-range controls using structured parameters
- worker timeout/result capture
- execution disabled by default
- loopback-only Docker Compose API
- focused CI tests

Standalone path:
`client -> Natron SaaS API -> durable job ledger -> NatronRenderer worker -> artifact`

Integrated path:
`client -> Caddy -> Kong -> Middleware -> Natron connector -> Natron SaaS API`

The native Natron renderer must never be exposed directly through Caddy or Kong. Large media should move through object storage rather than through Middleware.

Local API:
```bash
python3 -m venv .venv-saas
. .venv-saas/bin/activate
pip install -r requirements-saas.txt
export CODESTRA_AUTH_MODE=development
uvicorn codestra_saas:app --host 127.0.0.1 --port 8080
```

Native worker (only on a host with NatronRenderer installed):
```bash
export CODESTRA_EXECUTION_ENABLED=true
export CODESTRA_INPUT_ROOT=/srv/natron/inputs
export CODESTRA_OUTPUT_ROOT=/srv/natron/outputs
python codestra_saas.py --worker
```

Production certification is not claimed. Next hardening: PostgreSQL, object storage, signed events/webhooks, quotas/metering, Keycloak/OIDC, OpenBao secrets, metrics/traces, artifact QC, template registry, and Middleware/Kong/Caddy contract certification.
