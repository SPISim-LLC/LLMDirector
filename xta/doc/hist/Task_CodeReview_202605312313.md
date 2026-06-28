# Code Review Checklist — Refactor LLMDirector to Drive Conductor In-Process

> Companion to `Refactor_For_Director_FSD.md`. Use this to review the LLMDirector
> implementation against that FSD. Section numbers and requirement IDs (D-1…D-11)
> below trace to it; the imported library contract is its **Appendix A**. The
> Conductor-side review lives in `Refactor_For_Conductor_CodeReview.md`.
>
> **Acceptance gate:** every box checked, the unit suite green, and the dry-run
> reaches DONE via the in-process path **with the Conductor web app not running**.

---

## 1. Scope & blast radius (FSD §1, §2, §5)

- [x] Are **only** the five conductor-touching functions changed —
      `get_conductor_url` (deleted), `get_conductor_config`, `acquire_token`,
      `release_token`, `dispatch`, and the `/api/tmux` fallback in `get_tmux`?
- [x] Is everything in §5 genuinely **unchanged**: `transition()` + FSM graph,
      counters, `loopCap`, stagnation, `detect_outcome`, `check_for_event`,
      `tail_events`, `RunState` schema/`save`/`load`, hook preflight, event NDJSON,
      `LLMHookEvent.sh`, status-label mapping?
- [x] Are the only sanctioned dashboard-route changes the **two** named in §5 —
      the `/api/tmux` fallback body (§4.5) and the `/api/answer` guard (D-10) — with
      `/api/resume` **unchanged**?
- [x] Is the `requests` import to the Conductor gone (removed if otherwise unused)?

## 2. Import wiring (FSD §4.1 · D-1)

- [x] Is `…/LLMConductor` added to `sys.path` (via the existing symlink) and
      `import conductor_core` done once at startup?
- [x] Is there a **single** module-level `core_lock = conductor_core.DrivenLock()`
      shared across the process (not per-request)?
- [x] **D-1:** Does a failed `import conductor_core` **fail fast** at startup with a
      clear message (hard dependency, not an optional remote service)?

## 3. Config / topic resolution (FSD §4.2 · D-2)

- [x] Does `get_conductor_config()` call `conductor_core.get_config()` (cached), with
      no HTTP and no Java `/config` fallback?
- [x] **D-2a:** Does `load_config()` export the resolved `conductorJsonPath` into
      `os.environ['LLMC_CONFIG']` so `conductor_core.get_config()` reads the Director's
      configured file (not a CWD-relative `LLMConductor.json` default)?
- [x] **D-2:** Does dispatch use the **full** node string (`run.node`,
      `"02: Critique_Spec"`) and resolve role via `conductor_core.topic_role`? Is the
      stripped-suffix path impossible (and would surface as `UnknownTopic`)?

## 4. Lock lifecycle & reclaim (FSD §4.3 · D-3, D-3a, D-4, D-11)

- [x] **D-3:** Does `acquire_token()` **always** call `core_lock.take("LLMDirector",
      "local")` and adopt its result — **not** short-circuit on a persisted
      `controller_token`?
- [x] **D-3:** Are the three restart cases handled: lock still ours → reclaim & adopt;
      lock cleared → fresh token minted & adopted; lock held by another → `take()`
      returns `None` → escalate (no silent trust of the stale token)?
- [x] **D-3a:** Is `release_token()` shape-unchanged, and is releasing a non-matching
      token a safe no-op?
- [x] **D-4:** Is `release_if_last_run` (§5.6 release-on-last-terminal-run) unchanged?
- [x] **D-11:** Is the **single-Director-per-host** invariant real — does a second
      instance fail on `:8081` `EADDRINUSE`? Is reclaim-by-label justified only under
      it, with multi-instance identity called out as out-of-scope?

## 5. Dispatch (FSD §4.4 · D-5, D-6, D-8)

- [x] **D-5:** Is the marker/offset bookkeeping (watermark, marker ts, status
      transitions) byte-for-byte preserved so `check_for_event()` still matches?
- [x] **D-5:** Is `topic_role()` resolved **before** the dispatch marker is written,
      so an unknown-topic failure escalates without leaving a dangling
      awaiting-event marker?
- [x] **D-6:** Does the Director call `conductor_core.dispatch(cfg, project, target,
      run.node, message_override)` and rely on the library's `execute=True` default —
      passing **no** `skipPrePost` / `executeImmediately`? (A non-executing library
      would strand the run; confirm the dry-run actually advances.)
- [x] Is the `message_override` path preserved for human answers
      (`HUMAN_ANSWER_SENT_AWAITING_EVENT` status)?

## 6. Failure → escalation, never bare terminal `ERROR` (FSD §4.4 · D-8)

- [x] **D-8:** Do **all** dispatch-path failures route through the human-escalation
      path — `status="PAUSED_FOR_HUMAN"`, a concrete `escalation_kind`, and
      `notify_operator()` — mirroring `transition()`'s ERROR branch?
- [x] **D-8:** Is `status="ERROR"` (terminal, drops the lock via
      `release_if_last_run`) **never** set on a recoverable dispatch failure?
- [x] **D-8:** Are only **existing** escalation kinds reused — `TOKEN_FAILED` for the
      lock case, `ERROR` for `UnknownTopic` / `ok=False` / exception — with the
      specific cause in the decision log (no new escalation-kind vocabulary)?

## 7. Answer / resume guard (FSD §4.4 · D-10)

- [x] **D-10:** Does `/api/answer` return **`400` unless `escalation_kind ==
      "QUESTION"`**? (A typed reply must not be pasted into a pause with no pending
      question.)
- [x] **D-10:** Is `resume_run()` **unchanged** — no new per-kind behavior, no
      "resume re-enters the turn" for `QUESTION`?
- [x] **D-10:** Does the doc/UX make `/api/abort` the guaranteed recovery for
      `TOKEN_FAILED`/`ERROR`, with `/api/resume` correctly described as best-effort
      (re-dispatches only when `dispatch_marker.ts` is empty)?
- [x] Is the `resume_run` retry-for-repair-required item left **out of scope** (not
      silently implemented here)?

## 8. tmux capture fallback (FSD §4.5)

- [x] Does `/api/tmux` try the local `tmux capture-pane` first and fall back to
      `conductor_core.capture_pane(prj, targ)` (not an HTTP GET)?
- [x] Is the fallback failure surfaced cleanly (e.g. `502 {error}`) without crashing
      the route?

## 9. Config file & docs (FSD §4.6 · D-7)

- [x] Is `conductorUrl` **removed** from `LLMDirector.json`, with `conductorJsonPath`
      retained (consumed inside `conductor_core`)?
- [x] Is `LLMDirector.md` updated — §1 Prerequisites ("Conductor web app optional;
      `conductor_core` importable"), §2 Configuration, §9 Conductor lock (reclaim
      note), and the port map?
- [x] **D-7:** Can the Director drive a full run with the **Conductor web app not
      running at all**?

## 10. Imported contract conformance (FSD Appendix A)

- [x] Does the Director use only the Appendix A surface (`get_config`, `topic_role`,
      `topic_message`, `UnknownTopic`, `DrivenLock.take/release/is_valid/status/
      break_glass`, `dispatch`, `capture_pane`, `session_exists`, `DispatchResult`)?
- [x] Are the four contract assumptions relied upon and validated — `UnknownTopic`
      on bad key, `take()` reclaim by controller, `dispatch()` immediate-execute +
      forced `skip_pre_post`, `dispatch()` returns `ok=False` (not raise) on missing
      session?
- [x] If a checkout lacks the companion Conductor FSD, is Appendix A treated as the
      **normative** Director-side contract (acceptance does not depend on the
      companion file existing)?

## 11. Tests (FSD §7)

- [x] Are the existing 44 tests kept green, with the FSM/persistence behavior
      unchanged (dry-run trace matches Task 1)?
- [x] Are the HTTP-auth tests (`TestHTTPAuthProtocol`, X-Token header/body) replaced
      with **in-process** equivalents — asserting `conductor_core.dispatch`/`core_lock`
      are invoked with the right args, and that the lock token never leaves the
      process?
- [x] New: `dispatch` resolves role/target from the full node key and calls
      `conductor_core.dispatch` once with `run.node` (D-2/D-5)?
- [x] New: `dispatch` relies on the executing default; a stub that does not execute
      strands the run (D-6)?
- [x] New: `acquire_token` reconciles through `take()` on a simulated restart and
      adopts the returned token; a foreign-controller lock escalates (D-3)?
- [x] New: an unknown/short topic escalates to `PAUSED_FOR_HUMAN` (kind `ERROR`) with
      `notify_operator` fired — not terminal `ERROR`, not a silent empty dispatch
      (D-8)?
- [x] New: `/api/answer` returns `400` for a non-`QUESTION` escalation (D-10)?
- [x] End-to-end: dry-run reaches DONE with no Conductor web process; the Task-1
      20-turn run (escalation → answer → resume → Ready_To_Commit) reproduces
      in-process.

## 12. Migration hygiene (FSD §6)

- [x] Were the call sites swapped in the §6 order, and is `get_conductor_url`
      deleted?
- [x] Are there **no** leftover HTTP/token artifacts (X-Token headers, `requests`
      to `:8080`, `conductorUrl` reads)?
- [x] Is `_escalate`-style helper usage consistent with the existing
      `notify_operator` convention (no divergent ad-hoc error handling)?

## 13. Acceptance criteria (FSD §9 — final gate)

- [x] The five conductor-touching functions call `conductor_core` in-process; no
      `requests` call to the Conductor remains.
- [x] The Director drives a full dry-run to DONE with the Conductor **web app not
      running**.
- [x] A Director restart mid-run resumes via lock reclaim (reconcile through
      `take()`) — no `TOKEN_FAILED`, no manual break-glass; a foreign-controller lock
      escalates instead of trusting the stale token.
- [x] A short/unknown topic key fails loudly as a `PAUSED_FOR_HUMAN` escalation (kind
      `ERROR`, operator notified) — never terminal `ERROR`, never an empty dispatch.
- [x] Dispatch always executes immediately in-process (no message left un-sent).
- [x] The existing FSM/persistence/event behavior is byte-for-byte unchanged (unit
      suite green; dry-run trace matches Task 1).
