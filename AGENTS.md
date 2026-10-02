# Natron Repository Agent Contract

This repository is reserved for the governed Natron source/integration used by Codestra media workflows.

## Environment branch model

Promotion order is: `development` -> `testing` -> `staging` -> `production`.
`main` is the protected source-of-truth branch. Changes move between environment branches by reviewed pull request; do not force-push, rewrite history, or bypass required checks.

- Preserve upstream Natron license, provenance, and source history when importing.
- Keep Codestra-specific integrations isolated from upstream source whenever practical.
- Do not commit credentials or production publishing tokens.
- Test build/package changes before promotion.
