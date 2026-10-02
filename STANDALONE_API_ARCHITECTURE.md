# Natron Standalone Social Media VFX API

## Mission

Turn this repository into an independent, headless VFX, compositing, motion-graphics, and social-media rendering service.

Natron remains usable as a desktop compositing application, but automated execution must use the renderer and scripted project controls rather than GUI automation.

## Standalone rule

This repository must run without Kdenlive, without the Codestra Middleware, and without any social publisher.

It owns its own:
- REST API and OpenAPI contract
- worker/job execution
- PostgreSQL persistence
- Redis/queue integration
- object/artifact storage
- authentication/service authorization
- health/readiness endpoints
- logs, metrics, tracing
- Docker/runtime configuration
- tests and CI

No shared database and no direct source-code dependency on Kdenlive.

## Core responsibility

Natron is the VFX and motion-graphics engine:
- compositing
- animated titles
- lower thirds
- overlays
- logo animation
- keying/green-screen workflows
- masks/rotoscoping
- object/feature tracking
- background processing
- branding templates
- reusable social-media graphics
- rendered VFX plates and final compositions

Automated execution should use NatronRenderer and Natron Python/project scripting so requests can run headlessly.

## API surface

Minimum v1 contract:

- GET /healthz
- GET /readyz
- GET /v1/capabilities
- POST /v1/assets
- GET /v1/assets/{id}
- POST /v1/compositions
- GET /v1/compositions/{id}
- POST /v1/templates
- POST /v1/templates/{id}/render
- POST /v1/lower-thirds
- POST /v1/titles
- POST /v1/keying
- POST /v1/tracking
- POST /v1/masks
- POST /v1/background/remove
- POST /v1/overlays
- POST /v1/renders
- GET /v1/jobs/{id}
- POST /v1/jobs/{id}/cancel
- GET /v1/artifacts/{id}

Long-running work returns a job ID and executes asynchronously.

## AI VFX Director

AI must convert a prompt into a validated composition/edit graph or approved parameter changes. It must never receive permission to execute arbitrary shell commands.

Target capabilities:
- template selection
- title/lower-third generation
- mask suggestions
- object/face region selection
- background-removal assistance
- tracking setup assistance
- brand/logo placement
- animation parameter generation
- social-layout adaptation
- color/style suggestions
- composition QA

Generated plans must be schema validated and resolved only against an allow-listed node/template registry.

## Template system

Provide reusable templates for:
- social intro/outro
- lower thirds
- logo stings
- quote cards
- product showcases
- podcast/video overlays
- call-to-action animations
- captions/title treatments
- vertical-video overlays
- livestream graphics

Templates should expose validated parameters instead of requiring callers to modify raw Natron project internals.

Example:

POST /v1/templates/social-lower-third/render

{
  "name": "Jane Doe",
  "subtitle": "Product Director",
  "brand": "codestra",
  "aspect_ratio": "9:16"
}

## Social-media profile registry

Suggested profiles:

profiles/
- youtube-long.yaml
- youtube-short.yaml
- instagram-reel.yaml
- instagram-feed.yaml
- facebook-reel.yaml
- tiktok.yaml
- x-video.yaml
- linkedin-video.yaml

Use profiles for:
- frame size/aspect ratio
- frame rate
- safe zones
- title/overlay placement
- logo sizing
- alpha/export behavior
- quality-control expectations

## Quality certification

Before an artifact becomes READY:

1. renderer completed
2. output exists and is readable
3. frame size/aspect ratio validated
4. expected frame range/duration validated
5. alpha-channel expectation validated where required
6. missing frames detected
7. branding/template constraints validated
8. artifact checksum created
9. status = READY

Failures must expose machine-readable error codes.

## Events

Emit:
- job.accepted
- job.started
- job.progress
- job.completed
- job.failed
- artifact.ready
- render.certified

Support webhook and/or WebSocket consumers while keeping the core REST API independent.

## Optional integration

Natron must output ordinary media artifacts that any system can consume.

Typical combined flow:
AI/Client -> Natron -> Kdenlive -> QC -> Social Publisher

Standalone flow:
AI/Client -> Natron -> QC -> artifact consumer

Natron cannot require Kdenlive to be available.

## Repository implementation layout

Target layout:

api/
  routes/
  schemas/
  auth/
engine/
ai/
workers/
jobs/
templates/
profiles/
storage/
database/
events/
openapi/
tests/
docker/
Dockerfile
docker-compose.yml
AGENTS.md

## Environment branches

- main: protected stable source authority; no direct implementation pushes
- development: active integration and feature implementation
- testing: promoted candidates for automated/integration testing
- staging: release candidates that passed testing and are ready for staging validation
- production: production-qualified release state only

Promotion direction:

development -> testing -> staging -> production

External social publishing and production effects remain default-deny until explicitly enabled and certified.

## Initial implementation milestones

1. API skeleton + OpenAPI + health/readiness
2. durable jobs + queue + worker
3. asset storage
4. NatronRenderer adapter
5. Python/template parameter adapter
6. template registry
7. render/status/cancel APIs
8. QC pipeline
9. AI structured composition planner
10. social-media profiles
11. webhook/WebSocket events
12. Docker + CI + integration tests
