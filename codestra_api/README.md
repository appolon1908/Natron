# Codestra natron sidecar API

This directory provides the standalone local control surface for **codestra-natron-api**.

- Bind: `127.0.0.1:18103`
- Engine binary: `NatronRenderer`
- Media workspace: `/srv/codestra-video`
- Default concurrency: one render
- No shell execution
- Paths outside the media workspace are rejected
- Optional Bearer authentication: `CODESTRA_VIDEO_API_TOKEN`

Middleware V3 remains the durable command/idempotency/audit authority. This sidecar only starts, stops and reads back local engine processes.

## Contract

`GET /health`, `GET /status`, `GET /status/{job_id}`, `POST /render`, `POST /stop/{job_id}`.

Run the built-in guard test:

```bash
python3 server.py --self-test
```

Production/social publishing is not implemented here.
