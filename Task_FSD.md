# FSD - Deterministic Dispatch Events And File-Aware Transition Delay

> Scope: LLMDirector only. Do not change files under `./LLMConductor`.
> This task removes Native hook mode and replaces LLM-managed event retries with
> one dispatch-correlated event call plus a Director-managed, flow-driven delay.

## 1. Purpose

The current completion prompt asks the LLM to wait before calling the event
script, retry on failure, and stop after a configured number of attempts. This
is nondeterministic and does not solve the actual concurrency problem:
prompt-mode lookup identifies an active run by target alone, while different
projects commonly use the same target concurrently.

The new contract is:

1. LLMDirector creates a unique `dispatch_id` for every dispatched turn.
2. The appended prompt tells the LLM to call the event script exactly once with
   the dispatch id and canonical project directory.
3. The script writes and verifies the exact dispatch-correlated event.
4. LLMDirector matches events by `dispatch_id`.
5. When transition processing may depend on a file produced by the completed
   turn, LLMDirector waits `initialWaitSec` before evaluating the flow and
   dispatching the destination.

## 2. Scope And Non-Goals

- All implementation changes are confined to LLMDirector.
- Do not change `LLMConductor`, `conductor_core`, or `LLMConductor.json`.
- Do not change the lifecycle graph semantics.
- Do not add an event retry loop to the LLM prompt.
- Do not infer the run from target alone.
- Do not rely on the LLM shell's current directory.
- Do not change server-side `notifyScript` or `HumanNotifyScript` behavior,
  except for removing obsolete Native-mode documentation.

## 3. Remove Native Hook Mode

Native mode is no longer supported.

**Requirement N-1.** Remove the special `"Native"` value and all Native-mode
branches from configuration loading, validation, preflight, shutdown, and
documentation.

**Requirement N-2.** Remove `install_agent_hooks()`,
`uninstall_agent_hooks()`, `_cleanup_hooks()`, and their call sites.

**Requirement N-3.** Remove Native-hook parsing and deduplication from
`xta/bin/LLMHookEvent.sh`. The script supports only the prompt-driven invocation
defined in Section 6.

**Requirement N-4.** Remove tests and fixtures whose only purpose is Native hook
installation, cleanup, stdin parsing, or Native-mode selection.

**Requirement N-5.** `Hook` is always a script deployment path. An unusable path
remains a configuration/preflight error; there is no fallback mode.

**Requirement N-6.** Remove all Native-mode references from `LLMDirector.md`,
including prerequisites, configuration, start/abort/shutdown behavior, hook
cleanup, tests, and troubleshooting.

## 4. Configuration

`LLMDirector.json` retains only:

```json
"dispatchEventPrompt": {
  "initialWaitSec": 10
}
```

**Requirement C-1.** Remove `dispatchEventPrompt.retryWaitSec` from
configuration, code, documentation, and tests.

**Requirement C-2.** Remove `dispatchEventPrompt.maxAttempts` from
configuration, code, documentation, and tests.

**Requirement C-3.** `initialWaitSec` is a Director-side delay before
filesystem-dependent transition evaluation. It must never appear as an
instruction for the LLM to wait.

**Requirement C-4.** Missing `dispatchEventPrompt` or `initialWaitSec` defaults
to `10` seconds.

**Requirement C-5.** `initialWaitSec` must be a finite, non-negative number.
Boolean values are invalid. Invalid configuration must use the existing
config-error path and block new runs.

**Requirement C-6.** `initialWaitSec: 0` disables the delay without disabling
dispatch-id event correlation.

## 5. Dispatch Identity

**Requirement I-1.** Every call to `dispatch()`, including human-answer
dispatches, generates a fresh `dispatch_id` before prompt construction.

**Requirement I-2.** Use `uuid.uuid4().hex` as the dispatch-id format: 32
lowercase hexadecimal characters with no shell metacharacters.

**Requirement I-3.** Persist the id in `run.dispatch_marker["dispatch_id"]`.

**Requirement I-4.** `dispatch_marker` also persists the existing timestamp,
offset, and target plus the transition-delay fields defined in Section 8.

**Requirement I-5.** Older state files without `dispatch_id` must load without
crashing. Because Native mode is removed, an older in-flight awaiting-event
state without `dispatch_id` cannot be correlated safely and must escalate with
`ERROR` instructing the operator to resume/re-dispatch. It must not use legacy
target-only matching.

**Requirement I-6.** The escalation for an in-flight awaiting-event state without
`dispatch_id` happens immediately on state adoption or the first event-tail poll,
not after `TURN_TIMEOUT`. Waiting cannot make the uncorrelatable event safe.

**Requirement I-7.** Resuming that `ERROR` escalation clears the stale
`dispatch_marker`, keeps the run on the current node, and redispatches the
current topic. The redispatch creates a fresh `dispatch_id`.

## 6. Single-Call Event Script Contract

### 6.1 Positional syntax

The documented prompt-mode syntax is:

```bash
<Hook path> --prompt <TARGET> <EVENT> <DISPATCH_ID> <CANONICAL_CWD>
```

Example:

```bash
~/batch/LLMHookEvent.sh --prompt CODEX Stop 4d8f18a9b62c4d12a7f8365c913e2b40 /project/path
```

**Requirement H-1.** `--prompt` remains mandatory so malformed legacy/native
calls fail rather than being interpreted as valid prompt events.

**Requirement H-2.** The Director builds the command using `shlex.quote()` for
the Hook path, target, event, dispatch id, and canonical cwd.

**Requirement H-3.** The appended prompt tells the LLM to run the exact command
once as its final action and verify stdout contains `EVENT_SENT_OK`. If the
command does not print `EVENT_SENT_OK` and `HumanNotifyScript` is configured,
the prompt tells the LLM to run that notify script and stop.

**Requirement H-4.** The prompt contains no event wait, retry interval, retry
count, or max-attempt instruction.

**Requirement H-5.** The script rejects missing or extra positional arguments,
an empty target/cwd, an unsupported event, or a dispatch id not matching
`^[0-9a-f]{32}$`. Failure prints `EVENT_SEND_FAILED` and exits non-zero.

### 6.2 Event output and exact verification

**Requirement H-6.** The script writes one NDJSON event:

```json
{
  "ts": "...",
  "cwd": "...",
  "target": "CODEX",
  "event": "Stop",
  "dispatch_id": "..."
}
```

The obsolete `turn_id` field may be removed because it existed only for Native
hook deduplication.

**Requirement H-7.** The event filename is derived from the canonical cwd passed
by the Director. The script must not query `/api/runs` and must not fall back to
`pwd`.

The exact path is:

```text
<configured eventDir>/<sanitized canonical cwd>.ndjson
```

`eventDir` is injected into the deployed script from `LLMDirector.json`.
`sanitized canonical cwd` uses the same contract as `get_event_file(cwd)`:
resolve the canonical cwd, remove the leading slash, replace `/` with `_`, and
append `.ndjson`. The full event-file path is not passed in the prompt command;
only canonical cwd is passed.

**Requirement H-8.** Remove `DIRECTOR_URL` injection and the target-only
`/api/runs` lookup from the source and rendered script.

**Requirement H-9.** Preserve atomic append under the per-cwd `flock`.

**Requirement H-10.** Print `EVENT_SENT_OK` only after confirming the exact
`dispatch_id + cwd + target + event` record exists in the expected event file.
Otherwise print `EVENT_SEND_FAILED` and exit non-zero.

**Requirement H-11.** A repeated invocation with the same dispatch id must be
idempotent: do not append a duplicate exact event, but return `EVENT_SENT_OK` if
the exact event already exists. The prompt still instructs one call; this
protects manual accidental repetition.

**Requirement H-12.** Duplicate detection intentionally ignores `ts`. If an
existing event has the same `dispatch_id + cwd + target + event`, the script
returns `EVENT_SENT_OK` without appending a second line and without updating the
original timestamp.

**Requirement H-13.** If the one event-script call fails after the LLM has run
it, the Director treats that as a missing completion event. There is no
Director-visible failure signal until `limits.turnTimeoutSec` expires, at which
point the run escalates with `TURN_TIMEOUT`. Operator resume extends/retries per
the existing timeout recovery behavior; it does not assume a failed hook call
created a valid event.

## 7. Event Matching

**Requirement E-1.** `check_for_event(run)` matches only events whose
`dispatch_id` equals `run.dispatch_marker["dispatch_id"]`.

**Requirement E-2.** The matched event must also have the expected canonical
cwd, target, and supported event type. Dispatch id is the primary correlation
key; the remaining fields are consistency checks.

**Requirement E-3.** Events before `run.watermark`, malformed lines, missing
dispatch ids, wrong ids, or inconsistent fields must not transition the run.

**Requirement E-4.** Same-target concurrent runs in different projects must
remain independent.

**Requirement E-5.** Once an exact event is matched, persist
`dispatch_marker["event_matched_at"]` in UTC ISO format. Re-reading the same
event must not restart the delay.

## 8. Flow-Driven Transition Delay

### 8.1 Authoritative trigger

The existing expanded `LLMDirector_Flow.json` contains the file-dependency
information. No new delay metadata is required.

**Requirement D-1.** Apply `initialWaitSec` when transition processing for the
completed topic may read a sentinel produced or removed by that turn:

- the current topic entry has `backIf`;
- the current topic entry has `nextIf`;
- the current topic entry has `escalateIf`; or
- the universal `questionsSentinel` guard applies to the current topic.

This delay occurs before checking sentinel existence, stagnation, escalation, or
choosing a destination edge. It therefore protects both creation and deletion
of files that affect routing.

**Requirement D-2.** The universal questions guard applies when
`questionsSentinel` and `questionsBackTo` or `questionsEscalateTopics` are
configured and the current topic does not already declare that same sentinel in
`backIf` or `escalateIf`, matching the existing `transition()` behavior.

**Requirement D-3.** Do not delay for a topic whose transition performs no
configured sentinel read. In particular, unconditional `nextTo`,
`validate` pass/fail routing, and `commit_approval` do not independently trigger
the delay.

**Documented assumption A-1.** The flow sentinel fields are the authoritative
declaration that a completed LLM turn may leave a file needed for routing or by
the destination topic. The Director must not inspect prompt prose or compare
Architect/Developer roles to guess file dependencies.

### 8.2 Non-blocking delay state

**Requirement D-4.** On a matched event requiring delay, set
`dispatch_marker["transition_due_at"]` to
`event_matched_at + initialWaitSec` and persist it.

**Requirement D-5.** Do not call `sleep(initialWaitSec)` in the event-tail loop.
Each polling cycle checks due transitions so other runs continue processing.

**Requirement D-6.** While delayed, expose the run status text as:

```text
IN TRANSITION TO ...
```

The display may include the eventual destination once known without reading
sentinels early. Until routing is evaluated, the literal ellipsis is acceptable.
The persisted internal status must be explicit and must not contain
`AWAITING_EVENT`, so timeout processing cannot mistake it for a missing event.

**Requirement D-7.** When the due time is reached, call `transition(run)` once.
Clear the delay fields before or atomically with transition so a restart or later
poll cannot transition twice.

**Requirement D-8.** If no delay is required, or `initialWaitSec` is zero,
transition immediately after exact event matching.

## 9. Pause, Resume, Abort, Timeout, And Restart

**Requirement R-1.** `TURN_TIMEOUT` applies only before a matching event.
It must not fire in the transition-delay state.

**Requirement R-1a.** A prompt-mode script execution failure that does not write
a valid event is handled by the same pre-match timeout path. The Director cannot
distinguish "LLM still working" from "LLM ran the hook and it failed" until the
timeout expires.

**Requirement R-2.** Pause during delayed transition freezes progression. Resume
restores the transition-delay state and uses the original due time; if overdue,
transition on the next poll without redispatch.

**Requirement R-3.** Abort during delayed transition prevents any later
transition.

**Requirement R-4.** Restart during delayed transition restores
`event_matched_at`, `transition_due_at`, and the explicit transition state. It
waits only the remaining time and transitions immediately if overdue.

**Requirement R-5.** Human-answer dispatches use the same correlation and
flow-driven delay rules as normal dispatches.

## 10. Documentation

**Requirement DOC-1.** Update `LLMDirector.md` configuration examples and tables
to retain only `dispatchEventPrompt.initialWaitSec`.

**Requirement DOC-2.** Document `initialWaitSec` as a conditional,
Director-managed delay before sentinel-dependent transition evaluation.

**Requirement DOC-3.** Remove all LLM-side wait/retry/max-attempt language.

**Requirement DOC-4.** Document the positional single-call command,
dispatch-id correlation, canonical-cwd argument, exact verification, and
idempotent duplicate handling.

**Requirement DOC-5.** Remove all Native-mode and hook-cleanup documentation.

**Requirement DOC-6.** Document the dashboard state `IN TRANSITION TO ...`, the
flow sentinel conditions that trigger it, and restart/pause behavior.

## 11. Tests And Acceptance Criteria

**Requirement T-1. Config:** default, positive, zero, negative, boolean,
non-numeric, NaN, and infinity cases.

**Requirement T-2. Dispatch:** fresh 32-hex dispatch id on every normal and
human-answer dispatch; marker persistence; shell-safe positional command; no
wait/retry/max-attempt wording.

**Requirement T-3. Script:** strict arguments, canonical-cwd event file,
dispatch id in NDJSON, exact verification, failure exit, and duplicate
idempotency.

**Requirement T-4. Correlation:** same target across two projects cannot
cross-match; wrong/stale/malformed events are ignored.

**Requirement T-5. Delay trigger:** each of `backIf`, `nextIf`, `escalateIf`,
and universal questions guard causes delay; plain `nextTo`, validation, and
commit approval do not unless a sentinel rule also applies.

**Requirement T-6. Timing:** no early sentinel evaluation; transition occurs at
or after the due time; zero/no-delay transitions immediately; event-tail
processing for other runs remains active.

**Requirement T-7. Lifecycle:** pause, resume, abort, timeout, and restart obey
Section 9 without duplicate transition or redispatch.

**Requirement T-8. Removal:** no Native branches, agent settings mutations,
`DIRECTOR_URL`, target-only API lookup, `turn_id`, `retryWaitSec`, or
`maxAttempts` remain in production code, documentation, or active tests.

**Requirement T-9. Documentation:** `LLMDirector.md` accurately describes the
implemented configuration, command, flow-driven delay, status, and recovery
behavior.

**Acceptance command:** run the repository's authoritative Director test suite:

```bash
python3 xta/tst/RunTest.py -a
```

Acceptance requires a successful process exit and the suite's final failure
count to be zero.

## 12. Resolved Decisions

1. Native mode and all relevant code are removed.
2. The hook uses the documented positional syntax in Section 6.
3. Delayed runs display `IN TRANSITION TO ...`.
4. Delay is conditional and derived from existing flow sentinel metadata, not
   applied to every event.
5. The target-only `/api/runs` lookup and `DIRECTOR_URL` injection are removed.

## 13. Open Issues

No unresolved `[PEER]` or `[HUMAN]` questions remain. Assumption A-1 is explicit
and reviewable.

## 14. Peer Question Resolutions

The prior `TempTBD_Questions.md` review questions are resolved as follows:

1. Event files remain under configured `eventDir` and are named from sanitized
   canonical cwd: `<eventDir>/<sanitized canonical cwd>.ndjson`. The prompt
   command passes cwd, not the full event-file path.
2. Idempotency ignores `ts`; the stable identity is
   `dispatch_id + cwd + target + event`.
3. A failed one-shot script call is treated as a missing event and eventually
   escalates through `TURN_TIMEOUT`.
4. Legacy awaiting states without `dispatch_id` escalate immediately, and resume
   redispatches the current topic with a fresh dispatch id.
