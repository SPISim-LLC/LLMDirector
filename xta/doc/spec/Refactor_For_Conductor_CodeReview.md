# Code Review Checklist — Refactor LLMConductor into `conductor_core`

> Companion to `Refactor_For_Conductor_FSD.md`. Use this to review the LLMConductor
> implementation against the FSD. Section numbers and requirement IDs (C-1…C-9)
> below trace to that FSD. The matching Director-side review lives in
> `Refactor_For_Director_CodeReview.md` (when written).
>
> **Acceptance gate:** every box checked, the `conductor_core` unit suite and the
> HTTP parity suite green, and the dry-run reaches DONE via the in-process path.

---

## 1. Scope & structure (FSD §2, §4.1)

- [ ] Is **all** non-Flask logic moved out of `web/app.py` into `conductor_core/`
      (config, lock, topic resolution, compose, tmux I/O, dispatch, logging)?
- [ ] Does the package match the layout in §4.1 (`__init__.py`, `config.py`,
      `lock.py`, `tmux.py`, `compose.py`, `dispatch.py`)?
- [ ] Is the Java Conductor **untouched** (out of scope)?
- [ ] Is the on-disk lock-file format (`~/.llmconductor/driven.lock`,
      `{token,controller,host}`) unchanged?
- [ ] Is the import path documented and working — Director reaches `conductor_core`
      via the existing `LLMDirector/LLMConductor` symlink + `sys.path`, with the
      installable-package option noted as the durable path?

## 2. Topic-key contract (FSD §4.2 · C-1)

- [ ] Are `topic_keys`, `is_valid_topic`, `topic_role`, `topic_message` the **only**
      sanctioned way to resolve a topic, and is the old inline
      `cfg.get(topic).get('Mesg')` path gone?
- [ ] **C-1:** Do `topic_message`/`topic_role` raise `UnknownTopic` for any key not
      in `cfg` — including the stripped `"Critique_Spec"` vs `"02: Critique_Spec"`
      case? Is there **no** surviving `{}`→empty-string fallback?
- [ ] Is `UnknownTopic` a `KeyError` subclass (so existing `except KeyError` sites
      still behave) and does its message include the offending key?
- [ ] Is there a unit test that feeds a short key and asserts the raise (not an
      empty dispatch)?

## 3. Public API surface (FSD §4.3 · C-2)

- [ ] Does `conductor_core/__init__.py` re-export **exactly** the §4.3 surface
      (config/topics, lock lifecycle, `dispatch`, `capture_pane`, `session_exists`)?
- [ ] Is `DispatchResult` a dataclass with `ok`, `session`, `composed`, `error` —
      and never a Flask response object?
- [ ] **C-2:** Does `conductor_core` import **no** Flask and reference no
      `request`/`jsonify`? (grep the package — it must import cleanly in a bare
      Python REPL.)
- [ ] Can the whole package be imported and unit-tested **without** starting a
      server?

## 4. `DrivenLock` reclaim semantics (FSD §4.4 · C-3, C-4)

- [ ] **C-3:** Does `take()` by the controller that **already holds** the lock
      return the existing token (reclaim), not `None`/409?
- [ ] **C-3:** Does `take()` by a **different** controller still refuse (return
      `None` → 409 at the HTTP layer)?
- [ ] Are `status()`, `release(token)`, `break_glass()`, `is_valid(token)` present
      and backed by the shared lock file?
- [ ] **C-4:** Are all lock mutations guarded by a process-level `threading.Lock`
      (read-modify-write of the file is atomic within a process)?
- [ ] Is there a test for the reclaim path **and** the foreign-controller refusal?

## 5. `dispatch()` resolution point (FSD §4.5 · C-5, C-6)

- [ ] Does `dispatch()` resolve the body via `topic_message(cfg, topic)` unless a
      `message_override` is supplied? (**C-6** — unknown key raises, never empty.)
- [ ] **C-5:** Is `skip_pre_post` forced `True` on the DRIVEN dispatch path, with
      Pre-Script/Post-Prompt available **only** on the MANUAL `send()` path?
- [ ] Does it check `session_exists()` first and return `DispatchResult(ok=False,
      error=…)` when the tmux session is missing (no exception leak)?
- [ ] Is the tmux send/execute sequence (`load-buffer`→`paste-buffer`→optional
      `Enter`) preserved from the original `_do_send`?
- [ ] Is `log_entry(target, topic, "DRIVEN")` still emitted?

## 6. Flask layer is a thin adapter (FSD §4.6 · C-7)

- [ ] Does each route reduce to *read request → call `conductor_core` → jsonify*,
      with no business logic left in `app.py`?
- [ ] Do the lock endpoints share **one** module-level `DrivenLock` instance
      (not a fresh object per request)?
- [ ] **C-7:** Are all routes still present with unchanged method/path/schema:
      `/api/status`, `/api/config`, `/api/control/{take,release,break}`,
      `/api/dispatch`, `/api/send`, `/api/reload`, `/api/tmux`, `/api/logs*`,
      `/api/md`, `/`?
- [ ] Does `api_dispatch` map `UnknownTopic` → `400 {error}` and a missing session →
      `500 {error}` (per the adapter example), preserving the 200 success shape
      `{ok:"dispatched"}` for valid input?
- [ ] Is `get_auth_token` (header parsing) the **only** HTTP-auth concern left in the
      Flask layer?

## 7. Coexistence & concurrency (FSD §4.7 · C-8)

- [ ] **C-8:** Does `conductor_core` hold no in-memory authority that can contradict
      the lock file — is the **file** the single arbiter of MANUAL/DRIVEN?
- [ ] With the Director embedding the library **and** the Conductor web app running,
      does DRIVEN still block `/api/send` (manual) as today?
- [ ] Is it safe for two processes to load the module concurrently (no import-time
      global mutation of the lock/tmux that races)?

## 8. Backward-compatibility boundary (FSD §4.8 · C-7, C-9)

- [ ] Are there **exactly two** endpoint behavior deltas, and no others:
  - [ ] `POST /api/control/take`: owner re-take → `200 {token:<existing>}` (was 409);
  - [ ] `POST /api/dispatch`: unknown topic → `400 {error}` (was 200 + empty paste)?
- [ ] Do **all other** endpoints remain behavior-equivalent, including their
      error/edge inputs?
- [ ] **C-9:** Does the HTTP parity suite encode both deltas as *expected-change*
      assertions and assert full equivalence elsewhere?
- [ ] **C-9 (regression guard):** Does a test fail if either delta is reverted —
      i.e. a re-introduced silent empty dispatch, or `409` for the owner's own
      re-take?

## 9. Tests (FSD §6)

- [ ] Is there a new **`conductor_core` unit suite** with no Flask dependency
      covering: `UnknownTopic` on short keys (C-1/C-6), `topic_role`/`topic_message`
      correctness, `DrivenLock.take` reclaim vs foreign refusal (C-3),
      `dispatch()` error on missing session?
- [ ] Is there a **golden compose-parity** test: `compose_message` (and the moved
      `_expand_vars`/`_apply_lc`/`_normalize_for_tmux`/`_strip_trailing_newlines`/
      `_split_raw_cmd_lines`) produces byte-identical output to the pre-refactor
      code on a fixed corpus (incl. the `$PROJ`/`$TARG`/`lc()` and leading-`!`
      normalization edge cases)?
- [ ] Is there an **HTTP parity suite** (§8 above) for status codes + JSON shapes?
- [ ] Does the **Director dry-run** regression reach DONE through the in-process
      path?

## 10. Migration hygiene

- [ ] Are the helpers listed in §3/§5 of the FSD **moved verbatim** (not silently
      rewritten) — verified by the golden corpus before originals are deleted?
- [ ] Are the now-migrated functions **removed** from `web/app.py` (no dead
      duplicates that can drift)?
- [ ] Is `web/templates/index.html` unchanged (or, if touched, is the change
      unrelated and called out)?
- [ ] Does `web/app.py`'s `__main__` still `delete_lock()` + `load_config()` on
      startup and bind `0.0.0.0:8080`?

## 11. Acceptance criteria (FSD §8 — final gate)

- [ ] `conductor_core` imports with **no Flask dependency** and its unit suite passes.
- [ ] A short/unknown topic key raises `UnknownTopic` — no empty-body path remains.
- [ ] Same-controller `take()` reclaims; a different controller is refused.
- [ ] The standalone Conductor web app passes the HTTP parity suite (full
      equivalence except the two §4.8 deltas, which are asserted as expected).
- [ ] The Director drives a full dry-run to DONE via in-process calls **with the
      Conductor web app not running**.
