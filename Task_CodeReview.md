# Code Review Checklist - Deterministic Events And File-Aware Delay

> Review against `Task_FSD.md`. Findings must prioritize concurrency,
> filesystem-dependent routing, restart safety, and complete removal of obsolete
> Native/retry behavior.

## 1. Scope

- [ ] No files under `./LLMConductor` changed.
- [ ] `LLMConductor`, `conductor_core`, and lifecycle graph semantics are
      unchanged.
- [ ] Server-side human notification behavior remains intact.

## 2. Native Mode Removal

- [ ] `"Native"` is no longer accepted as a `Hook` mode.
- [ ] `Hook` is always validated and used as a deployable script path.
- [ ] `install_agent_hooks()`, `uninstall_agent_hooks()`, `_cleanup_hooks()`, and
      all call sites are removed.
- [ ] No code writes `.claude`, `.codex`, or `.gemini` hook settings.
- [ ] Native stdin parsing, `turn_id` extraction/deduplication, cleanup tests, and
      Native selection tests are removed.
- [ ] Invalid Hook paths fail through existing config/preflight handling without
      fallback.

## 3. Configuration

- [ ] `dispatchEventPrompt` contains only `initialWaitSec`.
- [ ] `retryWaitSec` and `maxAttempts` are absent from config, production code,
      documentation, and active tests.
- [ ] Missing `initialWaitSec` defaults to `10`.
- [ ] Zero and finite positive values are accepted.
- [ ] Negative, boolean, non-numeric, NaN, and infinity values block new runs
      through the config-error path.
- [ ] `initialWaitSec` never appears in the LLM completion instruction.

## 4. Dispatch Identity And Prompt

- [ ] Every normal and human-answer dispatch gets a fresh `uuid.uuid4().hex`.
- [ ] The 32-character lowercase hex id is persisted in `dispatch_marker`.
- [ ] Prompt command syntax is exactly documented as:
      `<Hook> --prompt <TARGET> <EVENT> <DISPATCH_ID> <CANONICAL_CWD>`.
- [ ] Every dynamic command argument is protected with `shlex.quote()`.
- [ ] The LLM is instructed to call the command once as its final action and
      verify `EVENT_SENT_OK`.
- [ ] If the command does not print `EVENT_SENT_OK` and `HumanNotifyScript` is
      configured, the prompt tells the LLM to notify the human and stop.
- [ ] No prompt wait, retry interval, retry loop, or max-attempt instruction
      remains.
- [ ] Existing `HumanNotifyScript` instructions remain correct.

## 5. Event Script

- [ ] Only the `--prompt` invocation is accepted.
- [ ] Missing/extra arguments, unsupported events, empty values, and malformed
      dispatch ids return `EVENT_SEND_FAILED` with non-zero exit.
- [ ] The passed canonical cwd determines the NDJSON filename.
- [ ] The event path is exactly
      `<configured eventDir>/<sanitized canonical cwd>.ndjson`, where
      sanitization matches `get_event_file(cwd)`.
- [ ] The full event-file path is not passed in the prompt command.
- [ ] The script never queries `/api/runs` and never falls back to `pwd`.
- [ ] `DIRECTOR_URL` rendering/injection is removed.
- [ ] Event JSON contains timestamp, cwd, target, event, and dispatch id.
- [ ] Atomic per-cwd `flock` append remains.
- [ ] `EVENT_SENT_OK` verifies the exact id/cwd/target/event record.
- [ ] Repeating the exact command is idempotent and does not append a duplicate.
- [ ] Idempotency ignores `ts`; existing id/cwd/target/event returns
      `EVENT_SENT_OK` without changing the original timestamp.
- [ ] A failed one-shot script call is not retried by the LLM prompt and is
      handled as missing event until `TURN_TIMEOUT`.

## 6. Event Correlation

- [ ] `check_for_event()` requires matching dispatch id.
- [ ] Cwd, target, and event are also checked for consistency.
- [ ] Watermark handling still excludes events from prior dispatches.
- [ ] Wrong-id, stale, malformed, or missing-id events do not transition.
- [ ] Two projects using the same target cannot consume each other's events.
- [ ] Older awaiting-event state without a dispatch id escalates clearly instead
      of falling back to target-only matching.
- [ ] That legacy-state escalation happens immediately on adoption or first poll,
      not after `TURN_TIMEOUT`.
- [ ] Resume from that legacy-state `ERROR` redispatches the current topic with a
      fresh dispatch id.
- [ ] Exact event match time is persisted once and cannot be reset by rereading.

## 7. Flow-Driven Delay Trigger

- [ ] Delay is derived from the expanded `LLMDirector_Flow.json`, not prompt
      prose, target role, or hardcoded topic names.
- [ ] A current-topic `backIf` triggers delay before sentinel evaluation.
- [ ] A current-topic `nextIf` triggers delay before sentinel evaluation.
- [ ] A current-topic `escalateIf` triggers delay before sentinel evaluation.
- [ ] The universal `questionsSentinel` guard triggers delay under the same
      applicability rule used by `transition()`.
- [ ] Both file creation and deletion are protected because no sentinel or
      stagnation check occurs before the delay expires.
- [ ] Plain `nextTo`, validation pass/fail, and commit approval do not trigger
      delay unless an applicable sentinel rule is also present.
- [ ] `initialWaitSec: 0` preserves correlation but transitions immediately.

## 8. Transition State And Timing

- [ ] Event matching stores `event_matched_at` and, when needed,
      `transition_due_at`.
- [ ] Delayed runs display `IN TRANSITION TO ...`.
- [ ] The persisted transition state does not contain `AWAITING_EVENT`.
- [ ] The event-tail thread never sleeps for the full transition delay.
- [ ] Other runs continue to receive event and timeout processing.
- [ ] `transition()` runs once at or after the due time.
- [ ] Delay fields are cleared safely so polling/restart cannot transition twice.
- [ ] No sentinel, stagnation, escalation, or destination-edge decision occurs
      before the due time.

## 9. Lifecycle Behavior

- [ ] `TURN_TIMEOUT` can fire only before exact event matching.
- [ ] Script execution failure without a valid event follows the same pre-match
      timeout path.
- [ ] Pause freezes delayed progression.
- [ ] Resume uses the original due time and never redispatches the completed turn.
- [ ] Abort prevents a pending delayed transition.
- [ ] Restart restores transition state and waits only the remaining duration.
- [ ] An overdue restored transition runs once on the next poll.
- [ ] Human-answer turns use identical correlation and delay behavior.

## 10. Documentation

- [ ] `LLMDirector.md` retains only `initialWaitSec` in config examples/tables.
- [ ] It defines `initialWaitSec` as a conditional Director-side delay.
- [ ] It documents the four flow sentinel conditions that trigger delay.
- [ ] It documents the positional command and single-call contract.
- [ ] It documents dispatch-id matching and exact script verification.
- [ ] It documents `IN TRANSITION TO ...`, pause/resume, and restart behavior.
- [ ] All Native-mode, hook-cleanup, target-only lookup, and LLM retry text is
      removed.

## 11. Tests

- [ ] Config tests cover default, positive, zero, negative, boolean, string,
      NaN, and infinity.
- [ ] Dispatch tests cover fresh ids, persistence, quoting, and prompt wording.
- [ ] Script tests cover validation, exact event, failure, and idempotency.
- [ ] Concurrency tests use two projects with the same target.
- [ ] Delay tests cover `backIf`, `nextIf`, `escalateIf`, universal questions,
      and no-delay edges.
- [ ] Timing tests prove no early file inspection and no event-loop blocking.
- [ ] Lifecycle tests cover pause, resume, abort, timeout, and restart.
- [ ] Removal tests or source assertions prove obsolete Native/retry/API lookup
      paths are gone.

## 12. Acceptance Criteria

- [ ] Every requirement in `Task_FSD.md` is implemented or explicitly rejected
      with a review finding.
- [ ] No unresolved `TempTBD_Questions.md` remains.
- [ ] `LLMDirector.md` matches actual behavior.
- [ ] The authoritative suite succeeds:

```bash
python3 xta/tst/RunTest.py -a
```

- [ ] The process exits successfully and the final failure count is zero.
