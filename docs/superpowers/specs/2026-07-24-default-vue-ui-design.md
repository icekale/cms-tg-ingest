# Vue UI Feature Parity and Default Entry

## Goal

Bring the Vue/Naive UI admin to feature parity with the existing server-rendered admin, then make `/app/` the default web entry point without changing task execution behavior.

## Design

- Add authenticated JSON actions for task retry, reprocess, Emby check, STRM restore, history cleanup, quality repair/settings/run, and HDHive subscription/settings/run/item confirmation.
- Extend JSON payloads so the Vue pages can show task timeline/observability, quality automation state, health wait details, and HDHive account/schedule/item data.
- Add buttons/forms in Vue for all existing server-rendered actions; action responses refresh the affected view and surface errors without navigating away.
- `GET /` returns a redirect to `/app/` after the existing web-token authorization flow.
- The existing server-rendered overview remains available at `/legacy` for rollback and compatibility.
- Existing server-rendered pages such as `/health`, `/quality`, `/hdhive`, and `/task/<id>` remain unchanged.
- `/app/` and `/api/v1/*` keep their current routing and authentication behavior.

## Verification

- Unit tests cover each JSON action's authorization, validation, state transition, and failure response, plus the root redirect and legacy overview route.
- Frontend tests continue to verify that the API-backed pages and action controls are present in the production build.
- The frontend build, Python test suite, and Docker build must pass.
- After deployment, `/`, `/app/`, and `/api/v1/health` must return successful responses.
