# DRYRUN Feature Specification — Hello World Health-Check Endpoint

> **Dry-run only.** This document is a minimal fake FSD used to exercise the
> LLMDirector FSM without running real LLM sessions. No real code changes are
> expected. All agents in this run execute `dryrun_agent.sh`.

---

## 1. Goal

Add a `/health` HTTP endpoint to the App service that returns `{"status":"ok"}`.

## 2. Acceptance criteria

- `GET /health` returns HTTP 200 with body `{"status":"ok"}`.
- No change to existing endpoints.
- Unit test added.

## 3. Non-goals

- Authentication on the health endpoint.
- Metrics or detailed diagnostics.

## 4. Implementation notes

Add one route handler and one test. No database access required.
