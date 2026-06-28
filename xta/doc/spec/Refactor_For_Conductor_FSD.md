# FSD — Refactor LLMConductor into an Importable Library (`conductor_core`)

> **Status:** Proposal (for later review). Author's preferred design for Task 2 —
> "make Conductor a library, not a service." This document specifies the changes
> required in the **Python LLMConductor** repo. Its companion,
> `Refactor_For_Director_FSD.md`, specifies the matching LLMDirector changes.
> Nothing here is implemented yet.

---

## 1. Purpose

Today LLMDirector drives LLMConductor over an HTTP+token REST protocol between two
co-located Python processes (Director :8081 → Conductor :8080). Every integration
bug we have hit lives at that seam:

- **Topic-key contract drift** — the Director sent a short topic name; the
  Conductor's `cfg.get(topic)` returned `{}` and silently dispatched an empty
  message body.
- **Distributed lock lifecycle** — the DRIVEN lock persists in
  `~/.llmconductor/driven.lock`, but the Director's token lives in its own process;
  a Director restart loses the token while the lock survives → `TOKEN_FAILED`.
- **"Which Conductor?"** — multiple endpoints/implementations of one role.

The fix is to convert the Conductor's capabilities from a **network service** into
an **in-process library** (`conductor_core`) that both (a) the existing standalone
Conductor web app and (b) the Director import and call directly. The network
boundary becomes a function-call boundary; the topic-key contract becomes shared
resolver functions; the lock becomes one object with well-defined reclaim
semantics.

This refactor must be **compatibility-preserving for the standalone Conductor**: its
web UI, the set and schemas of its REST endpoints, MANUAL/DRIVEN modes, and the
on-disk lock format keep working for human/manual use and any remote caller. Where a
correctness fix (C-1/C-3/C-6) unavoidably changes endpoint *behavior*, the precise
boundary — what "compatible" protects, and the exactly two sanctioned behavior
deltas — is defined in §4.8.

## 2. Scope

**In scope (this repo, `LLMConductor/`):**
- Extract all non-Flask logic from `web/app.py` into a new transport-agnostic
  module `conductor_core`.
- Define the public library API the Director will consume.
- Add a **topic-key contract** (resolver + validation) that makes the short-key bug
  structurally impossible.
- Turn the lock into a `DrivenLock` object with **idempotent same-controller take**
  (reclaim) to eliminate `TOKEN_FAILED`-on-restart.
- Re-skin `web/app.py` as a thin HTTP adapter over `conductor_core`.

**Out of scope:**
- The Java Conductor (untouched; this is the Python line only).
- Changing the on-disk lock-file format, the set of REST endpoints, or their
  request/response **schemas** (these stay backward compatible; the only sanctioned
  endpoint *behavior* changes are the two bounded in §4.8).
- The Director's FSM/transition logic (covered by the companion FSD).

## 3. Background — current structure (`web/app.py`)

Non-Flask logic currently interleaved with routes:

| Concern | Functions today |
|---|---|
| Config | `load_config`, `get_config` |
| Lock | `read_lock`, `write_lock`, `delete_lock`, `is_valid_token` |
| Topic→message | inline in `api_dispatch` (`cfg.get(topic).get('Mesg')`) |
| Message compose | `_compose_message`, `_expand_vars`, `_apply_lc`, `_normalize_for_tmux`, `_strip_trailing_newlines`, `_arr_to_text`, `_split_raw_cmd_lines` |
| Pre-script | `_run_pre_script` |
| tmux I/O | `_send_tmux_buffer`, `_do_send`, plus `capture-pane` in `api_tmux` |
| Logging | `log_entry` |
| HTTP only | `get_auth_token`, all `@app.route` handlers |

## 4. Target design

### 4.1 Module layout

Create a single-source, transport-agnostic package in the Conductor repo:

```
LLMConductor/
  conductor_core/
    __init__.py        # re-exports the public API (§4.3)
    config.py          # load/get config; topic-key contract (§4.2)
    lock.py            # DrivenLock object (§4.4)
    tmux.py            # session existence, send-buffer, capture-pane
    compose.py         # message composition helpers (moved verbatim)
    dispatch.py        # dispatch() / send() orchestration (§4.5)
  web/
    app.py             # thin Flask adapter over conductor_core (§4.6)
```

> **Import path for the Director.** The Director already symlinks
> `LLMDirector/LLMConductor -> ../LLMConductor`. The Director adds
> `…/LLMConductor` to `sys.path` and does `import conductor_core`. (Preferred
> long-term: publish `conductor_core` as an installable package both repos depend
> on via `pip install -e`; the symlink approach is the zero-packaging minimum and
> is acceptable for v1.)

### 4.2 Topic-key contract (kills the empty-message bug class)

`config.py` exposes the **only** sanctioned way to resolve a topic:

```python
def get_config() -> dict: ...
def load_config(path: str | None = None) -> dict: ...

def topic_keys(cfg) -> list[str]:            # cfg["Topic"], the full "NN: Name" strings
def is_valid_topic(cfg, key) -> bool:        # key in topic_keys(cfg) and key in cfg
def topic_role(cfg, key) -> str:             # cfg[key]["Role"]  ("Architect"|"Developer")
def topic_message(cfg, key) -> str:          # _arr_to_text(cfg[key]["Mesg"])

class UnknownTopic(KeyError): ...            # raised by resolvers on a bad/short key
```

**Requirement C-1.** `topic_message`/`topic_role` MUST raise `UnknownTopic` for any
key not present in `cfg` (e.g. the stripped `"Critique_Spec"` instead of
`"02: Critique_Spec"`). No silent `{}`→empty-string path may remain. This converts
the original production bug into a loud, immediate failure at the call site.

### 4.3 Public library API (`conductor_core/__init__.py`)

The Director and the Flask layer both consume exactly this surface:

```python
# config / topics
get_config(); load_config(path=None)
topic_keys(cfg); is_valid_topic(cfg, key); topic_role(cfg, key); topic_message(cfg, key)

# lock lifecycle  (in-process object backed by the shared lock file)
lock = DrivenLock()                  # see §4.4
lock.status()                        # {"mode","controller","token"}
lock.take(controller, host)          # -> token (idempotent for same controller)
lock.release(token)                  # -> bool
lock.break_glass()                   # force release
lock.is_valid(token)                 # bool

# dispatch / transport
dispatch(cfg, project, target, topic, message_override=None,
         execute=True) -> DispatchResult
capture_pane(project, target) -> str
session_exists(project, target) -> bool
```

`DispatchResult` is a small dataclass (`ok: bool`, `session: str`,
`composed: str | None`, `error: str | None`) — never a Flask response.

**Requirement C-2.** `conductor_core` MUST NOT import Flask or reference
`request`/`jsonify`. It is pure library code, unit-testable without a server.

### 4.4 `DrivenLock` — reclaim semantics (kills `TOKEN_FAILED`-on-restart)

`lock.py` wraps the existing `~/.llmconductor/driven.lock` file (same JSON shape:
`{token, controller, host}`), but `take()` becomes **idempotent for the same
controller**:

```python
def take(self, controller, host) -> str | None:
    cur = self._read()
    if cur is None:
        token = uuid4(); self._write(token, controller, host); return token
    if cur["controller"] == controller:        # same controller reconnecting
        return cur["token"]                     # reclaim — do NOT 409
    return None                                 # held by a *different* controller
```

**Requirement C-3.** A `take()` by the controller that already owns the lock MUST
return the existing token (reclaim), not an error. A `take()` by a different
controller MUST still be refused. This lets a Director restart re-adopt its own
lock without a manual break-glass.

**Requirement C-4.** All lock mutations MUST be guarded by a process-level
`threading.Lock` (the Director may call from its event-tail thread while Flask
serves a request in another).

### 4.5 `dispatch()` — single resolution point

`dispatch.py::dispatch()` folds today's `api_dispatch` topic-resolution + `_do_send`
into one transport-agnostic call:

```python
def dispatch(cfg, project, target, topic, message_override=None, execute=True):
    msg = message_override if message_override is not None else topic_message(cfg, topic)
    composed = compose_message(cfg, project, target, msg, skip_pre_post=True)
    if not session_exists(project, target):
        return DispatchResult(ok=False, error=f"tmux session missing: {target}_{project}")
    send_buffer(f"{target}_{project}", composed)
    if execute: send_enter(f"{target}_{project}")
    log_entry(target, topic, "DRIVEN")
    return DispatchResult(ok=True, session=f"{target}_{project}", composed=composed)
```

**Requirement C-5.** In DRIVEN mode `skip_pre_post` stays forced `True` (preserves
current behavior). Pre-Script/Post-Prompt remain available only on the MANUAL
`send()` path.

**Requirement C-6.** `dispatch()` MUST resolve the message via `topic_message`
(§4.2), so an unknown/short topic key raises `UnknownTopic` rather than dispatching
an empty body.

### 4.6 Flask layer becomes a thin adapter (`web/app.py`)

Each route shrinks to: read request → call `conductor_core` → `jsonify`. The lock
endpoints share **one module-level `DrivenLock` instance**. Examples:

```python
core_lock = conductor_core.DrivenLock()

@app.route('/api/dispatch', methods=['POST'])
def api_dispatch():
    if not core_lock.is_valid(get_auth_token()):
        return jsonify({'error': 'Unauthorized'}), 403
    d = request.get_json(force=True)
    try:
        r = conductor_core.dispatch(conductor_core.get_config(),
                                    d.get('project',''), d.get('target',''),
                                    d.get('topic',''), d.get('messageOverride'))
    except conductor_core.UnknownTopic as e:
        return jsonify({'error': f'unknown topic: {e}'}), 400
    return (jsonify({'ok':'dispatched'}) if r.ok else (jsonify({'error':r.error}), 500))
```

**Requirement C-7.** All existing REST routes (`/api/status`, `/api/config`,
`/api/control/{take,release,break}`, `/api/dispatch`, `/api/send`, `/api/reload`,
`/api/tmux`, `/api/logs*`, `/api/md`, `/`) MUST remain present with unchanged method,
path, and request/response **schema**. Their observable behavior MUST be equivalent
for the previously-correct input domain, with the **exactly two** sanctioned deltas
on `/api/control/take` and `/api/dispatch` defined in §4.8 — so remote/manual callers
and the browser UI are unaffected except where a latent bug is corrected.

### 4.7 Coexistence model (two processes, one substrate)

After the refactor the Director embeds `conductor_core` and no longer needs the
Conductor web app to be running in order to dispatch. The standalone Conductor web
app stays available as the **human/manual viewer** over the *same* substrate:

- Single source of truth for control is the shared `driven.lock` file.
- While the Director holds the lock (DRIVEN), the Conductor UI shows DRIVEN and its
  `/api/send` stays refused — unchanged from today.
- The human uses the Conductor UI only in MANUAL mode (Director idle / lock
  released), or the break-glass button to reclaim.

**Requirement C-8.** `conductor_core` MUST be safe for two processes to load
concurrently: it holds no exclusive in-memory authority that contradicts the lock
file; the file is the arbiter.

### 4.8 Backward-compatibility boundary (resolves the C-7 vs C-1/C-3/C-6 conflict)

> **Decision (Architect, Answer_Update_Spec).** The Developer correctly noted that
> "REST behavior stays unchanged" (§1, §2, C-7) literally contradicts the
> reclaim (C-3) and `UnknownTopic` (C-1/C-6) requirements, which *do* change the
> behavior of `/api/control/take` and `/api/dispatch`. **Resolution: the correctness
> requirements win.** "Backward compatible" is the narrower, well-defined contract
> below; it never protects a latent bug or gratuitous strictness.

"Backward compatible" / "behavior-equivalent" (§1, §2, C-7) is defined as the
conjunction of:

- **(a) Endpoint set unchanged** — every route in C-7 still exists with the same
  method and path.
- **(b) Schema unchanged** — request fields, response JSON shapes/keys, and the HTTP
  status *vocabulary* (200/400/403/409/500) are unchanged.
- **(c) Behavior preserved for the previously-correct input domain** — any request
  that, under the current Conductor, produced a *correct* result yields the same
  result and status after the refactor.

Compatibility does **not** protect (i) behavior that was a latent bug (a 200
"success" that pasted nothing) or (ii) behavior that was gratuitously strict against
the system's own controller. Where C-1/C-3/C-6 conflict with a literal reading of
"behavior-equivalent," the correctness requirement wins and the change is confined to
the previously-broken/edge input domain.

**Exactly two** endpoint behavior deltas are sanctioned; no others are permitted:

| Endpoint | Old behavior (specific input) | New behavior | Why it does not break the meaningful contract |
|---|---|---|---|
| `POST /api/control/take` | `take` by the controller that **already holds** the lock → `409 {error:"Already locked by another"}` | `200 {token:<existing>}` (reclaim, C-3) | Strictly more permissive, confined to the same-controller case; a **different** controller still gets `409`. Schema and status vocabulary unchanged. No distinct caller that previously succeeded now fails. |
| `POST /api/dispatch` | unknown/short `topic` → `200 {ok:"dispatched"}` while pasting an **empty** body (silent no-op) | `400 {error:"unknown topic: <key>"}` (C-1/C-6) | Corrects a silent failure masquerading as success. For every **valid** topic the behavior and status are identical. |

All other endpoints — `/api/status`, `/api/config`, `/api/control/{release,break}`,
`/api/send`, `/api/reload`, `/api/tmux`, `/api/logs*`, `/api/md`, `/` — remain
behavior-equivalent in full, including for their error/edge inputs.

**Requirement C-9.** The HTTP parity suite (§6) MUST encode these two deltas as
explicit expected-change assertions and assert full equivalence for every other
endpoint. A regression that reverts either delta (e.g. re-introducing the silent
empty dispatch, or `409` for the owner's own re-take) MUST fail the suite.

## 5. File-by-file change summary

| File | Change |
|---|---|
| `conductor_core/__init__.py` | New. Re-export public API (§4.3). |
| `conductor_core/config.py` | New. `load/get_config` + topic contract (§4.2). |
| `conductor_core/lock.py` | New. `DrivenLock` with reclaim (§4.4). |
| `conductor_core/tmux.py` | New. `session_exists`, `send_buffer`, `send_enter`, `capture_pane` (moved from `_do_send`/`api_tmux`). |
| `conductor_core/compose.py` | New. Move `_compose_message`, `_expand_vars`, `_apply_lc`, `_normalize_for_tmux`, `_strip_trailing_newlines`, `_arr_to_text`, `_split_raw_cmd_lines`, `_run_pre_script` verbatim. |
| `conductor_core/dispatch.py` | New. `dispatch()`, `send()` (§4.5). |
| `web/app.py` | Rewrite routes as thin adapters; delete the migrated helpers; one shared `DrivenLock`. |
| `web/templates/index.html` | Unchanged. |

## 6. Test plan

- **New unit suite** `conductor_core` (no Flask): topic resolver raises
  `UnknownTopic` on short keys (C-1/C-6); `topic_role`/`topic_message` correctness;
  `DrivenLock.take` reclaim for same controller, 409-equivalent for another (C-3);
  `compose_message` parity vs current output on a fixed corpus; `dispatch()` returns
  `error` when session missing.
- **HTTP parity tests:** existing endpoint contracts unchanged (status codes +
  JSON shapes) — assert `/api/dispatch`, `/api/control/*`, `/api/status`,
  `/api/tmux` behave identically before/after.
- **Regression:** the Director dry-run (companion FSD §7) must reach DONE using the
  in-process path.

## 7. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Two processes driving tmux/lock race | File-lock is arbiter; `threading.Lock` per process (C-4); tmux sends are short and serialized per process. |
| Import-path fragility via symlink | Document `sys.path` insert; offer installable-package path as the durable option. |
| Hidden behavior in `_compose_message`/`_normalize_for_tmux` | Move **verbatim**; add a golden corpus parity test before deleting originals. |
| REST drift breaks remote callers | C-7 parity tests gate the change. |

## 8. Acceptance criteria

1. `conductor_core` imports with **no Flask dependency** and passes its unit suite.
2. Resolving a short/unknown topic key raises `UnknownTopic` (no empty-body path).
3. Same-controller `take()` reclaims the lock; different controller is refused.
4. Standalone Conductor web app passes the HTTP parity suite: full equivalence on
   every endpoint except the two §4.8 deltas, which are asserted as expected changes.
5. The Director (companion FSD) drives a full dry-run to DONE via in-process calls,
   with the Conductor web app **not running**.
