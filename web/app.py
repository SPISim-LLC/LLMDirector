import argparse, atexit, json, os, re, subprocess, time, threading, hashlib, shlex
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, render_template, request, send_from_directory

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "LLMConductor"))
    import conductor_core
except ImportError as e:
    sys.exit(f"FATAL: Could not import conductor_core. LLMConductor must be accessible at LLMDirector/LLMConductor. Error: {e}")

core_lock = conductor_core.DrivenLock()
app = Flask(__name__)
_config = None
_flow_config = None        # loaded LLMDirector_Flow.json
_flow_config_error = None  # validation error string; None = valid
_run_states = {}
_lock = threading.Lock()
_validation_lock = threading.Lock()
_conductor_config_cache = None

ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
KNOWN_ACTIONS = {"validate", "validate_report", "commit_approval"}
KNOWN_ESCALATION_KINDS = {
    "QUESTION", "COMMIT_APPROVAL", "LOOP_CAP", "STAGNATION",
    "MAX_TURNS", "ERROR", "TURN_TIMEOUT", "TOKEN_FAILED"
}
TS_FMT = "%Y-%m%d-%H:%M:%S"   # YYYY-MMDD-HH:mm:ss  e.g. 2026-0602-14:30:15

# Decision-log verbs that precede a topic name; padded to the longest so the
# topic column lines up across line types.
_LOG_VERBS = ("Dispatching", "Transition from")
_LOG_VERB_W = max(len(v) for v in _LOG_VERBS) + 1


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_ts(dt=None):
    if dt is None:
        dt = datetime.now().astimezone()
    elif dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime(TS_FMT)

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def next_local_time_utc(hour, minute):
    """Next occurrence of local wall-clock HH:MM as a UTC ISO instant.
    Rolls to the next day if that time has already passed today."""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("hour/minute out of range")
    now_local = datetime.now().astimezone()
    target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_local:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def parse_iso_timestamp(ts):
    if not ts:
        raise ValueError("missing timestamp")
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    # Older state/log data may be naive; preserve compatibility by treating it as UTC.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

def strip_ansi(text): return ANSI_ESCAPE.sub('', text)

def parse_fails(stdout):
    clean = strip_ansi(stdout)
    matches = list(re.finditer(r"^\s*FAIL\s*:\s*(\d+)\s*$", clean, re.MULTILINE))
    return int(matches[-1].group(1)) if matches else -1


def _fsd_declares_no_suite(cwd_path, marker):
    """True if the run's FSD file contains the explicit no-test-suite marker.
    The marker must appear verbatim; prose is never matched."""
    if not marker:
        return False
    try:
        fsd_name = conductor_core.key_files(get_conductor_config()).get('FSD_FILE', 'Task_FSD.md')
    except Exception:
        fsd_name = 'Task_FSD.md'
    fsd = Path(cwd_path) / fsd_name
    try:
        return fsd.exists() and marker in fsd.read_text(encoding='utf-8')
    except Exception:
        return False


def _fsd_test_roster(cwd_path, marker_prefix):
    """Return the scoped acceptance roster the FSD pins, or None for the full suite.

    The FSD may declare that acceptance runs a scoped roster rather than the full
    -a suite, via an explicit marker, e.g.
        <!-- LLMDIRECTOR: TEST_ROSTER=xta/tst/TaskTests.txt -->
    so the validate gate runs `RunTest.py --file <roster>` (the FSD's own
    acceptance command). Deterministic marker only — prose is never matched."""
    if not marker_prefix:
        return None
    try:
        fsd_name = conductor_core.key_files(get_conductor_config()).get('FSD_FILE', 'Task_FSD.md')
    except Exception:
        fsd_name = 'Task_FSD.md'
    fsd = Path(cwd_path) / fsd_name
    try:
        if not fsd.exists():
            return None
        for line in fsd.read_text(encoding='utf-8').splitlines():
            idx = line.find(marker_prefix)
            if idx == -1:
                continue
            rest = line[idx + len(marker_prefix):].split('-->', 1)[0].strip()
            return rest or None
    except Exception:
        return None
    return None


def _write_validation_result(path, fails, returncode, summary, tail):
    """Record the Director-run validation outcome for the Architect to judge.

    This is pure fact-collection — it never decides pass/fail. The Architect
    (the judge topic) reads this file and routes the run. `fails` is the parsed
    'FAIL : N' count (-1 when no parseable summary line was emitted, e.g. the
    harness crashed); `returncode` is None when RunTest.py could not be launched."""
    lines = ["# Validation Result — Director-run RunTest.py", ""]
    if summary:
        lines.append(summary)
    if returncode is not None:
        lines.append(f"- RunTest.py exit code: {returncode}")
    if fails is not None and fails >= 0:
        lines.append(f"- Parsed failure count: FAIL : {fails}")
    elif returncode is not None:
        lines.append("- No parseable 'FAIL : N' summary line was emitted "
                     "(the harness may not have completed — consider an environment/build problem).")
    lines.append("- Full report: xta/tst/TestResult.html")
    if tail:
        lines += ["", "## Last lines of RunTest.py output", "```", tail.strip(), "```"]
    try:
        path.write_text("\n".join(lines) + "\n", encoding='utf-8')
    except Exception:
        pass


# ── RunState ──────────────────────────────────────────────────────────────────

class RunState:
    def __init__(self, project, cwd, arch, dev, start_node=None):
        self.project = project; self.cwd = cwd; self.arch = arch; self.dev = dev
        default_start = _flow_config['startTopic'] if _flow_config else "02: Critique_Spec"
        self.start_node = start_node or default_start
        self.node = self.start_node
        self.status = "RUNNING"
        self.total_turns = 0
        self.started_at = utc_now_iso()
        self.roles_initialized = {}        # {"Developer": True, "Architect": False, ...}
        self.pending_workflow_node = None  # legacy (pre-bundling); retained for state-file compat
        self.counters = {}                 # dynamic; keyed by loopCounter values
        self.history = []; self.watermark = 0
        self.dispatch_marker = {"ts": "", "offset": 0, "target": ""}
        self.controller_token = None
        self.escalation_kind = None
        self.escalation_detail = ""        # human-facing reason (e.g. which file to delete)
        self.stagnation_hashes = {}
        self.scheduled_for = None          # UTC ISO instant for a deferred start, else None
        self.resume_at = None              # UTC ISO instant for a scheduled PAUSED_BY_USER resume
        self.resume_send_command = None    # tmux command to send when scheduled resume fires

    def to_dict(self): return self.__dict__.copy()

    def save(self):
        if not _config: return
        sanitized = self.cwd.replace('/', '_').strip('_')
        path = Path(_config['logDir']) / (sanitized + ".state.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f: json.dump(self.to_dict(), f)

    @classmethod
    def load(cls, cwd):
        if not _config: return None
        sanitized = cwd.replace('/', '_').strip('_')
        path = Path(_config['logDir']) / (sanitized + ".state.json")
        if not path.exists(): return None
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        run = object.__new__(cls)
        run.project = data['project']; run.cwd = data['cwd']
        run.arch = data['arch']; run.dev = data['dev']
        default_start = _flow_config['startTopic'] if _flow_config else "02: Critique_Spec"
        run.node = data.get('node', default_start)
        # Older states predate per-run start selection; they always started at the global s0.
        run.start_node = data.get('start_node', default_start)
        run.status = data.get('status', 'RUNNING')
        run.total_turns = data.get('total_turns', 0)
        run.started_at = data.get('started_at', utc_now_iso())
        run.roles_initialized = data.get('roles_initialized', {})
        run.pending_workflow_node = data.get('pending_workflow_node', None)
        run.counters = data.get('counters', {})
        run.history = data.get('history', [])
        run.watermark = data.get('watermark', 0)
        run.dispatch_marker = data.get('dispatch_marker', {"ts": "", "offset": 0, "target": ""})
        run.controller_token = data.get('controller_token', None)
        run.escalation_kind = data.get('escalation_kind', None)
        run.escalation_detail = data.get('escalation_detail', "")
        run.stagnation_hashes = data.get('stagnation_hashes', {})
        run.scheduled_for = data.get('scheduled_for', None)
        run.resume_at = data.get('resume_at', None)
        run.resume_send_command = data.get('resume_send_command', None)
        return run


# ── Config loading and flow-config validation ─────────────────────────────────

def expand_path(p):
    if not p: return p
    return str(Path(p).expanduser().resolve())

def load_config(path):
    global _config, _flow_config_error
    config_dir = Path(path).resolve().parent
    try:
        with open(path, 'r', encoding='utf-8') as f: _config = json.load(f)
    except Exception as e:
        _flow_config_error = f"Failed to parse {path}: {e}"
        return _config

    val = _config.get('dispatchEventPrompt', {}).get('initialWaitSec', 10)
    if isinstance(val, bool) or not isinstance(val, (int, float)) or val != val or val == float('inf') or val < 0:
        _flow_config_error = f"dispatchEventPrompt.initialWaitSec must be a finite, non-negative number, got {val}"
        return _config

    _config['eventDir'] = expand_path(_config['eventDir'])
    _config['logDir'] = expand_path(_config['logDir'])
    cjp = _config.get('conductorJsonPath', '')
    if cjp and not Path(cjp).is_absolute():
        _config['conductorJsonPath'] = str((config_dir / cjp).resolve())
    if 'conductorJsonPath' in _config:
        os.environ['LLMC_CONFIG'] = _config['conductorJsonPath']
    fjp = _config.get('directorFlowJsonPath', '')
    if fjp and not Path(fjp).is_absolute():
        _config['directorFlowJsonPath'] = str((config_dir / fjp).resolve())

    # Resolve Hook and HumanNotifyScript
    repo_root = Path(__file__).resolve().parent.parent
    hook = _config.get('Hook', '~/batch/LLMHookEvent.sh')
    if hook.lower() == 'native':
        _flow_config_error = "Hook mode 'Native' is no longer supported"
        return _config
    hook = os.path.expandvars(os.path.expanduser(hook))
    if not Path(hook).is_absolute():
        hook = str((repo_root / hook).resolve())
    _config['Hook'] = hook

    hns = _config.get('HumanNotifyScript', '~/batch/LLMWaiting.bat')
    hns = os.path.expandvars(os.path.expanduser(hns))
    if not Path(hns).is_absolute():
        hns = str((repo_root / hns).resolve())
    _config['HumanNotifyScript'] = hns

    return _config

def get_dispatch_event_prompt_config():
    prompt_cfg = _config.get('dispatchEventPrompt', {}) if _config else {}
    return {
        "initialWaitSec": prompt_cfg.get('initialWaitSec', 10)
    }

def get_conductor_config():
    global _conductor_config_cache
    if _conductor_config_cache is None:
        _conductor_config_cache = conductor_core.get_config()
    return _conductor_config_cache

def _conductor_topic_set(cfg):
    return set(cfg.get("Topic", []))

def _mesg_text(cfg, topic_id):
    parts = cfg.get(topic_id, {}).get("Mesg", [])
    if isinstance(parts, list): return " ".join(parts)
    return str(parts)

def _reachable_from(flow, start):
    """BFS over *To edges (backTo, nextTo, passTo, failTo, nextIfTo)."""
    visited = set(); queue = [start]
    while queue:
        node = queue.pop(0)
        if node in visited: continue
        visited.add(node)
        entry = flow.get('topics', {}).get(node, {})
        for f in ('backTo', 'nextTo', 'passTo', 'failTo', 'nextIfTo'):
            dest = entry.get(f)
            if dest: queue.append(dest)
    return visited

def validate_flow_config(flow, conductor_cfg):
    """Return list of error strings; empty list means valid."""
    errors = []

    # Requirement H-9: Validate Hook at startup
    if _config:
        hook = _config.get("Hook", "")
        if hook:
            hook_path = Path(hook)
            # Check if directory exists and is writable (no side effects)
            parent = hook_path.parent
            if parent.exists():
                if not os.access(parent, os.W_OK):
                    errors.append(f"Hook directory '{parent}' is not writable")
            else:
                # Find closest existing parent
                curr = parent
                while not curr.exists() and curr.parent != curr:
                    curr = curr.parent
                if not os.access(curr, os.W_OK):
                    errors.append(f"Cannot create Hook in '{parent}'; parent '{curr}' is not writable")

    conductor_topics = _conductor_topic_set(conductor_cfg)

    start = flow.get('startTopic')
    end   = flow.get('endTopic')
    topics = flow.get('topics', {})

    if not start: errors.append("Missing required field: startTopic")
    if not end:   errors.append("Missing required field: endTopic")
    if not isinstance(topics, dict): errors.append("'topics' must be an object"); return errors

    if start and start not in conductor_topics:
        errors.append(f"startTopic '{start}' not in LLMConductor.json Topic list")
    if end and end not in conductor_topics:
        errors.append(f"endTopic '{end}' not in LLMConductor.json Topic list")

    # questionsBackTo must exist in both LLMConductor.json and the companion topics map
    qb = flow.get('questionsBackTo')
    if qb:
        if qb not in conductor_topics:
            errors.append(f"questionsBackTo '{qb}' not in LLMConductor.json Topic list")
        if qb not in topics:
            errors.append(f"questionsBackTo '{qb}' has no entry in companion topics map")
    qe_topics = flow.get('questionsEscalateTopics', [])
    for t in qe_topics:
        if t not in conductor_topics:
            errors.append(f"questionsEscalateTopics entry '{t}' not in LLMConductor.json Topic list")

    # questionsSentinel must be a safe relative path if declared
    qs = flow.get('questionsSentinel')
    if qs is not None:
        qsp = Path(qs)
        if qsp.is_absolute() or '..' in qsp.parts:
            errors.append(f"questionsSentinel '{qs}' must be a safe relative path")

    # requiredFiles safety check
    for rf in flow.get('requiredFiles', []):
        rp = Path(rf)
        if rp.is_absolute() or '..' in rp.parts:
            errors.append(f"requiredFiles entry '{rf}' must be a safe relative path")

    # Validate each configured topic entry
    for tid, entry in topics.items():
        ctx = f"topic '{tid}'"
        if tid not in conductor_topics:
            errors.append(f"{ctx}: not in LLMConductor.json Topic list"); continue

        # Dispatchable topics must have Role Architect or Developer
        role = conductor_cfg.get(tid, {}).get('Role', '')
        if role not in ('Architect', 'Developer'):
            errors.append(f"{ctx}: Role must be 'Architect' or 'Developer', got '{role}'")

        # *To destinations must exist
        for dest_field in ('backTo', 'nextTo', 'passTo', 'failTo', 'nextIfTo'):
            dest = entry.get(dest_field)
            if dest and dest not in conductor_topics:
                errors.append(f"{ctx}: {dest_field} '{dest}' not in LLMConductor.json Topic list")

        # backIf/nextIf must pair with backTo/nextIfTo respectively
        if 'backIf' in entry and 'backTo' not in entry:
            errors.append(f"{ctx}: backIf declared without backTo")
        if 'nextIf' in entry and 'nextIfTo' not in entry:
            errors.append(f"{ctx}: nextIf declared without nextIfTo")

        # escalateIf must pair with escalateKind
        if 'escalateIf' in entry and 'escalateKind' not in entry:
            errors.append(f"{ctx}: escalateIf declared without escalateKind")

        # Sentinel paths must be relative, no path traversal
        for sf in ('backIf', 'nextIf', 'escalateIf'):
            sentinel = entry.get(sf)
            if not sentinel: continue
            sp = Path(sentinel)
            if sp.is_absolute() or '..' in sp.parts:
                errors.append(f"{ctx}: {sf} '{sentinel}' must be a safe relative path")
                continue
            # Sentinel must be mentioned (case-sensitive substring) in topic's Mesg
            mesg = _mesg_text(conductor_cfg, tid)
            if sentinel not in mesg:
                errors.append(f"{ctx}: {sf} '{sentinel}' not mentioned in topic Mesg text")

        # Known action values
        action = entry.get('action')
        if action and action not in KNOWN_ACTIONS:
            errors.append(f"{ctx}: unknown action '{action}'; known: {sorted(KNOWN_ACTIONS)}")

        # validate action requires loopCounter and passTo/failTo
        if action == 'validate':
            if not entry.get('loopCounter'):
                errors.append(f"{ctx}: action 'validate' requires loopCounter field")
            if not entry.get('passTo'):
                errors.append(f"{ctx}: action 'validate' requires passTo field")
            if not entry.get('failTo'):
                errors.append(f"{ctx}: action 'validate' requires failTo field")

        # validate_report runs the suite as pure fact-collection and always hands
        # the result to the next topic (the Architect judges it); it requires nextTo.
        if action == 'validate_report' and not entry.get('nextTo'):
            errors.append(f"{ctx}: action 'validate_report' requires nextTo field")

        # Known escalateKind values
        ek = entry.get('escalateKind')
        if ek and ek not in KNOWN_ESCALATION_KINDS:
            errors.append(f"{ctx}: unknown escalateKind '{ek}'; known: {sorted(KNOWN_ESCALATION_KINDS)}")

    # Reachability: every configured topic must be reachable from startTopic
    if start and not errors:
        reachable = _reachable_from(flow, start)
        for tid in topics:
            if tid not in reachable:
                errors.append(f"Configured topic '{tid}' is unreachable from startTopic '{start}'")
        if end and end not in reachable:
            errors.append(f"endTopic '{end}' is unreachable from startTopic '{start}'")
        # Every topic reachable from startTopic must have an entry
        for tid in reachable:
            if tid not in topics:
                errors.append(f"Reachable topic '{tid}' has no entry in topics map")

    # entryTopics: each must be a real, routed topic that can still reach endTopic
    for et in flow.get('entryTopics', []):
        if et not in conductor_topics:
            errors.append(f"entryTopics entry '{et}' not in LLMConductor.json Topic list")
        if et not in topics:
            errors.append(f"entryTopics entry '{et}' has no entry in companion topics map")
        elif end and end not in _reachable_from(flow, et):
            errors.append(f"entryTopics entry '{et}': endTopic '{end}' is unreachable from it")

    return errors

def load_flow_config():
    global _flow_config, _flow_config_error
    if _flow_config_error is not None:
        return
    path_str = _config.get('directorFlowJsonPath', '') if _config else ''
    if not path_str:
        _flow_config_error = "directorFlowJsonPath not configured in LLMDirector.json"
        return
    path = Path(path_str)
    if not path.exists():
        _flow_config_error = f"Flow config file not found: {path}"
        return
    try:
        with open(path, 'r', encoding='utf-8') as f:
            flow = json.load(f)
    except Exception as e:
        _flow_config_error = f"Failed to parse {path}: {e}"
        return
    try:
        conductor_cfg = get_conductor_config()
    except Exception as e:
        _flow_config_error = f"Failed to load LLMConductor.json: {e}"
        return
    # Expand $KEY placeholders using Key Files from LLMConductor.json before validation
    # so sentinel filenames match the expanded Mesg text from LLMConductor.
    kf = conductor_core.key_files(conductor_cfg)
    if kf:
        combined = {'Key Files': kf}
        combined.update(flow)
        flow = conductor_core.expand_key_file_placeholders(combined)
        flow.pop('Key Files', None)
    errs = validate_flow_config(flow, conductor_cfg)
    if errs:
        _flow_config_error = "Flow config validation errors:\n" + "\n".join(f"  - {e}" for e in errs)
        return
    _flow_config = flow
    _flow_config_error = None


# ── Token / lock helpers ───────────────────────────────────────────────────────

def acquire_token(run):
    tok = core_lock.take("LLMDirector", "local")
    if not tok: return None
    if tok != run.controller_token:
        run.controller_token = tok; run.save()
    return tok

def release_token(run):
    if run and run.controller_token:
        try: core_lock.release(run.controller_token)
        except Exception: pass
        run.controller_token = None; run.save()

def _all_runs_terminal():
    for r in _run_states.values():
        if r.status not in ("DONE", "ABORTED", "ERROR"): return False
    return True

def release_if_last_run(run=None):
    if _all_runs_terminal():
        t = run if (run and run.controller_token) else next(
            (r for r in _run_states.values() if r.controller_token), None)
        if t: release_token(t)


# ── Notifications and logging ─────────────────────────────────────────────────

def notify_operator(run, kind):
    if not _config: return
    raw_template = _config.get('notifyScript')
    if not raw_template: return
    replacements = {"$PROJ": run.project, "$TOPIC": run.node, "$KIND": kind,
                    "$STATUS": run.status, "$CWD": run.cwd,
                    "$TARG": run.dispatch_marker.get("target", "")}
    parts = shlex.split(raw_template)
    if not parts: return
    parts[0] = str(Path(parts[0]).expanduser().resolve())
    for i in range(len(parts)):
        for k, v in replacements.items(): parts[i] = parts[i].replace(k, str(v))
    if os.path.exists(parts[0]):
        if len(parts) == 1: parts.extend([run.project, kind, run.status, run.cwd])
        try:
            # Fire-and-forget: don't wait for the script — it contains blocking
            # commands (nc UDP) that outlast any reasonable timeout.
            devnull_fd = open(os.devnull, 'w')
            subprocess.Popen(parts, stdout=devnull_fd, stderr=devnull_fd,
                             stdin=subprocess.DEVNULL, start_new_session=True,
                             close_fds=True)
            log_decision(run, f"Notify        {kind}  script launched")
        except Exception as e:
            log_decision(run, f"Notify        {kind}  FAILED: {e}")

def log_decision(run, message):
    entry = fmt_ts() + " | " + message
    run.history.append(entry)
    if _config:
        log_path = Path(_config['logDir']) / (run.cwd.replace('/', '_').strip('_') + ".log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, 'a', encoding='utf-8') as f: f.write(entry + "\n")
    run.save()


# ── Progress helpers ──────────────────────────────────────────────────────────

def _shortest_path(flow, start=None, end=None):
    """BFS from start to end (default flow start/end); returns ordered topic IDs."""
    start = start or flow['startTopic']; end = end or flow['endTopic']
    queue = [(start, [start])]; visited = set()
    while queue:
        node, path = queue.pop(0)
        if node == end: return path
        if node in visited: continue
        visited.add(node)
        entry = flow.get('topics', {}).get(node, {})
        for f in ('nextTo', 'passTo', 'backTo', 'failTo', 'nextIfTo'):
            dest = entry.get(f)
            if dest and dest not in visited:
                queue.append((dest, path + [dest]))
    return [start, end]

def entry_topics_ordered(flow):
    """Curated start-topic menu in the order the config author declared it.

    Driven by the optional top-level `entryTopics` array. When absent, only the
    global startTopic is offered (backward-compatible: behaves as before). The
    startTopic is always a valid entry point and is listed first.
    """
    start = flow['startTopic']
    entries = list(flow.get('entryTopics') or [])
    if start not in entries:
        entries.insert(0, start)
    return list(dict.fromkeys(entries))

def _anchored_topic_step(flow, path, node):
    """Pin off-path loop nodes to the latest on-path milestone that can reach them."""
    try:
        return path.index(node), False
    except ValueError:
        pass
    for idx in range(len(path) - 1, -1, -1):
        if node in _reachable_from(flow, path[idx]):
            return idx, True
    return -1, True

def get_run_progress(run):
    """Return progress dict for dashboard display."""
    if not _flow_config:
        return {}
    run_start = getattr(run, 'start_node', None) or _flow_config['startTopic']
    path = _shortest_path(_flow_config, start=run_start)
    total = max(len(path) - 1, 1)
    step, off_path = _anchored_topic_step(_flow_config, path, run.node)

    max_turns = (_config.get('limits', {}).get('maxTurns', 40) if _config else 40)
    loop_cap  = (_config.get('limits', {}).get('loopCap', 5)   if _config else 5)

    entry = _flow_config.get('topics', {}).get(run.node, {})
    lc = entry.get('loopCounter')
    loop_val = run.counters.get(lc, 0) if lc else None

    # Validate is run by the Director itself (RunTest.py), not by the dispatched
    # Architect/Developer agent. Flag when it is actively executing so the UI can
    # make the actor unambiguous.
    action = entry.get('action')
    v_started = run.dispatch_marker.get('v_started') if isinstance(run.dispatch_marker, dict) else None
    validating = bool(
        action in ('validate', 'validate_report')
        and run.status == 'IN TRANSITION TO ...'
        and v_started
    )
    # Elapsed seconds since the Director launched RunTest.py (v_started carries the
    # launch instant). Only meaningful while validating.
    validation_elapsed_s = None
    if validating and isinstance(v_started, str):
        try:
            vt0 = parse_iso_timestamp(v_started)
            validation_elapsed_s = int((datetime.now(timezone.utc) - vt0.astimezone(timezone.utc)).total_seconds())
        except Exception:
            validation_elapsed_s = None

    # Elapsed seconds (whole run)
    try:
        started = parse_iso_timestamp(run.started_at)
        elapsed_s = int((datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds())
    except Exception:
        elapsed_s = 0

    # Elapsed seconds for the current topic, anchored to its dispatch time.
    topic_ts = run.dispatch_marker.get('ts') if isinstance(run.dispatch_marker, dict) else None
    topic_elapsed_s = None
    if topic_ts:
        try:
            t0 = parse_iso_timestamp(topic_ts)
            topic_elapsed_s = int((datetime.now(timezone.utc) - t0.astimezone(timezone.utc)).total_seconds())
        except Exception:
            topic_elapsed_s = None

    return {
        "topicStep": step,
        "topicTotal": total,
        "totalTurns": run.total_turns,
        "maxTurns": max_turns,
        "loopCounter": lc,
        "loopVal": loop_val,
        "loopCap": loop_cap,
        "elapsedSec": elapsed_s,
        "startedAt": fmt_ts(parse_iso_timestamp(run.started_at)) if run.started_at else "",
        "topicElapsedSec": topic_elapsed_s,
        "topicStartedAt": fmt_ts(parse_iso_timestamp(topic_ts)) if topic_ts else "",
        "validating": validating,
        "validationElapsedSec": validation_elapsed_s,
        "offPath": off_path,
        "questionsSentinel": (_flow_config.get('questionsSentinel', 'TempTBD_Questions.md')
                              if _flow_config else 'TempTBD_Questions.md'),
    }


# ── Hook management ───────────────────────────────────────────────────────────

def get_event_file(cwd):
    sanitized = str(Path(cwd).resolve()).replace('/', '_').strip('_')
    return Path(_config['eventDir']) / (sanitized + ".ndjson")


def _render_hook_script():
    """Repo LLMHookEvent.sh with the configured eventDir injected."""
    repo_hook = Path(__file__).resolve().parent.parent / "xta" / "bin" / "LLMHookEvent.sh"
    src = repo_hook.read_text(encoding='utf-8')
    event_dir = _config.get("eventDir", "") if _config else ""
    src = src.replace('EVENT_DIR=""', f'EVENT_DIR="{event_dir}"', 1)
    return src

def _deploy_hook_script(dest):
    """Write the rendered LLMHookEvent.sh to dest, but only if missing or stale
    (Requirement H-5: do not rewrite when already current). chmod 755."""
    dest = Path(dest)
    desired = _render_hook_script()
    if dest.exists() and dest.read_text(encoding='utf-8') == desired:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(desired, encoding='utf-8')
    dest.chmod(0o755)

def required_files():
    """Required task files from flow config, falling back to conductor key files."""
    req = _flow_config.get('requiredFiles') if _flow_config else None
    if req is not None:
        return req
    try:
        kf = conductor_core.key_files(get_conductor_config())
        return [kf.get('FSD_FILE', 'Task_FSD.md'), kf.get('REVIEW_FILE', 'Task_CodeReview.md')]
    except Exception:
        return ['Task_FSD.md', 'Task_CodeReview.md']

def missing_required_files(cwd_path):
    """First required file absent under cwd_path, or None if all present."""
    for rf in required_files():
        if not (Path(cwd_path) / rf).exists():
            return rf
    return None

def preflight_hooks(cwd, arch, dev):
    if not _config: return True
    hook = _config.get("Hook", "")
    try:
        _deploy_hook_script(hook)
    except Exception as e:
        raise RuntimeError(f"Failed to deploy hook script at '{hook}': {e}")
    return True


# ── Stagnation ────────────────────────────────────────────────────────────────

def node_sentinels(run):
    """Resolved sentinel filenames tracked for the current node (real filenames;
    flow config is expanded at load). Shared by stagnation detection and the
    resume-from-stagnation file check."""
    if not _flow_config: return set()
    entry = _flow_config.get('topics', {}).get(run.node, {})
    # Sentinels declared in config for this topic. escalateIf only counts as a
    # stagnation signal when its escalation is STAGNATION; paired with another
    # kind (e.g. ERROR at the commit node) it drives its own escalation and must
    # not be pre-empted by the stagnation guard, which runs first.
    sentinels = set(s for s in [entry.get('backIf'), entry.get('nextIf')] if s)
    if entry.get('escalateIf') and entry.get('escalateKind') == 'STAGNATION':
        sentinels.add(entry['escalateIf'])
    # Always track the universal questions sentinel when questionsBackTo is configured
    qs = _flow_config.get('questionsSentinel', 'TempTBD_Questions.md')
    if _flow_config.get('questionsBackTo') and qs:
        sentinels.add(qs)
    return sentinels

def detect_stagnation(run):
    if not _flow_config: return False
    sentinels = node_sentinels(run)
    if not sentinels: return False

    cwd_path = Path(run.cwd); present = []; signals = []
    for s in sorted(sentinels):
        p = cwd_path / s
        if p.exists(): present.append(s); signals.append(s + ":" + p.read_text())
    blob = "".join(signals)
    if not blob:
        run.stagnation_hashes[run.node] = ""; return []
    h = hashlib.md5(blob.encode()).hexdigest()
    if run.stagnation_hashes.get(run.node) == h:
        return present   # the unchanged sentinel file(s) that caused the stall
    run.stagnation_hashes[run.node] = h; return []


# ── Transition ────────────────────────────────────────────────────────────────

def _escalate(run, kind, detail=""):
    if run.status == "PAUSED_FOR_HUMAN" and run.escalation_kind == kind and run.escalation_detail == detail:
        return # Already escalated with this reason
    log_decision(run, f"Escalate      {kind}" + (f": {detail}" if detail else ""))
    run.status = "PAUSED_FOR_HUMAN"; run.escalation_kind = kind
    run.escalation_detail = detail
    run.save()  # Commit status before non-idempotent notification
    notify_operator(run, kind)

def transition(run):
    """Config-driven FSM transition. Reads flow config + filesystem; no outcome argument."""
    if not _flow_config:
        _escalate(run, "ERROR", "flow config not loaded"); return

    entry = _flow_config['topics'].get(run.node)
    if entry is None:
        _escalate(run, "ERROR", f"no routing entry for topic '{run.node}'"); return

    limits = _config.get('limits', {}) if _config else {}
    max_turns = limits.get('maxTurns', 40)
    loop_cap  = limits.get('loopCap', 5)
    cwd_path  = Path(run.cwd)

    # 1. Max-turn guard
    if run.total_turns >= max_turns:
        _escalate(run, "MAX_TURNS"); return

    # 2. Stagnation guard
    stag_files = detect_stagnation(run)
    if stag_files:
        _escalate(run, "STAGNATION",
                  detail="no progress; delete to continue: " + ", ".join(stag_files)); return

    # 3. Universal questions guard (topics not declaring it as per-topic sentinel)
    questions_back    = _flow_config.get('questionsBackTo')
    questions_escalate = set(_flow_config.get('questionsEscalateTopics', []))
    q_sentinel = _flow_config.get('questionsSentinel', 'TempTBD_Questions.md')
    q_file = cwd_path / q_sentinel
    # Only apply when current topic doesn't already declare questions handling in config
    topic_declares_questions = (
        entry.get('escalateIf') == q_sentinel or entry.get('backIf') == q_sentinel
    )
    if not topic_declares_questions and q_file.exists():
        if run.node in questions_escalate:
            _escalate(run, "QUESTION"); return
        elif questions_back and run.node != questions_back:
            run.node = questions_back; dispatch(run); return

    # 4. Action: validate_report — Director runs the suite as pure fact-collection.
    #    It executes RunTest.py, records the outcome into a sentinel for the Architect
    #    to judge, and ALWAYS advances to nextTo. It never decides pass/fail and never
    #    escalates on a test FAIL; interpretation belongs to the Architect (judge topic).
    action = entry.get('action')
    if action == 'validate_report':
        # Crash sentinel: v_started set but we re-entered means a prior run was
        # interrupted mid-flight (process restart) — surface for human verification.
        if run.dispatch_marker.get('v_started'):
            _escalate(run, "ERROR",
                      "Validation interrupted by crash/restart. Please verify state and resume."); return
        run.dispatch_marker['v_started'] = utc_now_iso()  # launch instant (also the crash sentinel)
        run.save()

        result_file = cwd_path / (entry.get('resultFile') or 'TempTBD_ValidationResult.md')
        no_suite_marker = entry.get('noSuiteMarker')
        if no_suite_marker and _fsd_declares_no_suite(cwd_path, no_suite_marker):
            log_decision(run, "Validation skipped — FSD declares no test suite (marker present)")
            _write_validation_result(result_file, fails=0, returncode=0,
                                     summary="Skipped — FSD declares no test suite (marker present).", tail="")
        else:
            with _validation_lock:
                test_script = cwd_path / "xta" / "tst" / "RunTest.py"
                if not test_script.exists():
                    run.dispatch_marker.pop('v_started', None); run.save()
                    _escalate(run, "ERROR", "xta/tst/RunTest.py not found"); return
                roster = _fsd_test_roster(cwd_path, entry.get('rosterMarker'))
                if roster:
                    if not (cwd_path / roster).exists():
                        run.dispatch_marker.pop('v_started', None); run.save()
                        _escalate(run, "ERROR", f"FSD test roster not found: {roster}"); return
                    test_cmd = ["python3", "xta/tst/RunTest.py", "--file", roster]
                    log_decision(run, f"Validation scoped to FSD roster: RunTest.py --file {roster}")
                else:
                    test_cmd = ["python3", "xta/tst/RunTest.py", "-a"]
                try:
                    validation_timeout = limits.get('validationTimeoutSec', 1200)
                    res = subprocess.run(test_cmd, cwd=run.cwd,
                                         capture_output=True, text=True, timeout=validation_timeout)
                except Exception as e:
                    # A harness crash/timeout is still reported (not escalated) so the
                    # Architect can judge env-break vs code-break and route accordingly.
                    res = None
                    _write_validation_result(result_file, fails=-1, returncode=None,
                                             summary=f"Validation could not run: {e}", tail="")
                    log_decision(run, f"Validation could not run: {e}")
                if res is not None:
                    fails = parse_fails(res.stdout)
                    _write_validation_result(result_file, fails=fails, returncode=res.returncode,
                                             summary=None, tail=(res.stdout or "")[-4000:])
                    log_decision(run,
                                 f"Validation recorded: exit={res.returncode}, "
                                 f"FAIL={fails if fails >= 0 else 'n/a'} (Architect to judge)")
        run.dispatch_marker.pop('v_started', None)
        run.save()
        run.node = entry['nextTo']; dispatch(run); return

    # 4b. Action: validate (legacy self-judging gate: Director decides pass/fail)
    if action == 'validate':
        fails = run.dispatch_marker.get('v_fails')
        if fails is None:
            if run.dispatch_marker.get('v_started'):
                _escalate(run, "ERROR", "Validation interrupted by crash/restart. Please verify state and resume.")
                return

            run.dispatch_marker['v_started'] = utc_now_iso()  # launch instant (also the crash sentinel)
            run.save() # Commit start intent

            # The FSD may declare that this task has no test suite to run (e.g. a
            # pure-visual change, or a feature whose only relevant tests are
            # unrelated pre-existing failures). Honor that ONLY via an explicit,
            # deterministic marker in the FSD — never by pattern-matching prose —
            # so skipping the regression suite is always a conscious decision.
            no_suite_marker = entry.get('noSuiteMarker')
            if no_suite_marker and _fsd_declares_no_suite(cwd_path, no_suite_marker):
                log_decision(run, "Validation skipped — FSD declares no test suite (marker present)")
                run.dispatch_marker['v_fails'] = 0
                run.dispatch_marker.pop('v_started', None)
                run.save()
            else:
                with _validation_lock:
                    test_script = cwd_path / "xta" / "tst" / "RunTest.py"
                    if not test_script.exists():
                        _escalate(run, "ERROR", "xta/tst/RunTest.py not found"); return
                    # The FSD may pin acceptance to a scoped roster (RunTest.py --file
                    # <roster>) rather than the full -a suite. Honor that ONLY via an
                    # explicit marker. A declared-but-missing roster is a real failure
                    # (fail-safe), never a silent fall-back to -a.
                    roster = _fsd_test_roster(cwd_path, entry.get('rosterMarker'))
                    if roster:
                        if not (cwd_path / roster).exists():
                            _escalate(run, "ERROR", f"FSD test roster not found: {roster}"); return
                        test_cmd = ["python3", "xta/tst/RunTest.py", "--file", roster]
                        log_decision(run, f"Validation scoped to FSD roster: RunTest.py --file {roster}")
                    else:
                        test_cmd = ["python3", "xta/tst/RunTest.py", "-a"]
                    try:
                        validation_timeout = limits.get('validationTimeoutSec', 1200)
                        res = subprocess.run(test_cmd,
                                             cwd=run.cwd, capture_output=True, text=True, timeout=validation_timeout)
                        fails = parse_fails(res.stdout)
                    except Exception as e:
                        _escalate(run, "ERROR", f"validation execution failed: {e}"); return
                # FSD acceptance requires a successful process exit; a non-zero exit is a
                # real validation failure regardless of whether a FAIL line was emitted.
                if res.returncode != 0:
                    _escalate(run, "ERROR", f"validation process exited {res.returncode}"); return
                # A clean exit with no FAIL line means there was nothing to validate
                # (e.g. the FSD declares this task has no RunTest suite to run) — pass.
                if fails < 0:
                    fails = 0
                run.dispatch_marker['v_fails'] = fails
                run.dispatch_marker.pop('v_started', None)
                run.save()  # Persist result before evaluated transition side effects

        # Evaluation phase (idempotent relative to v_fails marker)
        v_result = run.dispatch_marker.pop('v_fails')
        if v_result == 0:
            run.node = entry['passTo']; dispatch(run); return
        else:
            lc = entry.get('loopCounter')
            if lc:
                run.counters[lc] = run.counters.get(lc, 0) + 1
                if run.counters[lc] >= loop_cap: _escalate(run, "LOOP_CAP"); return
            run.node = entry['failTo']; dispatch(run); return

    # 5. Action: commit_approval
    if action == 'commit_approval':
        _escalate(run, "COMMIT_APPROVAL"); return

    # 6. escalateIf
    ei = entry.get('escalateIf')
    if ei and (cwd_path / ei).exists():
        _escalate(run, entry.get('escalateKind', 'QUESTION')); return

    # 7. backIf  (increment loopCounter if incrementLoopCounter set, mirroring nextIf)
    bi = entry.get('backIf')
    if bi and (cwd_path / bi).exists():
        lc = entry.get('loopCounter')
        if lc and entry.get('incrementLoopCounter'):
            run.counters[lc] = run.counters.get(lc, 0) + 1
            if run.counters[lc] >= loop_cap: _escalate(run, "LOOP_CAP"); return
        run.node = entry['backTo']; dispatch(run); return

    # 8. nextIf / nextIfTo  (increment loopCounter if incrementLoopCounter set)
    ni = entry.get('nextIf')
    if ni and (cwd_path / ni).exists():
        lc = entry.get('loopCounter')
        if lc and entry.get('incrementLoopCounter'):
            run.counters[lc] = run.counters.get(lc, 0) + 1
            if run.counters[lc] >= loop_cap: _escalate(run, "LOOP_CAP"); return
        run.node = entry['nextIfTo']; dispatch(run); return

    # 9. Unconditional nextTo
    nt = entry.get('nextTo')
    if nt:
        run.node = nt; dispatch(run); return

    # 10. Terminal: endTopic reached with no onward routing → run complete
    if run.node == _flow_config.get('endTopic'):
        run.status = "DONE"
        log_decision(run, "Run Completed")
        release_if_last_run(run); run.save()
        return

    # No routing configured
    _escalate(run, "ERROR", f"no routing destination for topic '{run.node}'")


# ── Dispatch ──────────────────────────────────────────────────────────────────

def _get_pre_prompt_text(cfg):
    parts = cfg.get('Default', {}).get('Pre-Prompt', [])
    if isinstance(parts, list): return "\n\n".join(parts)
    return str(parts)

def dispatch(run, message_override=None):
    cfg = get_conductor_config()
    token = acquire_token(run)
    if not token:
        _escalate(run, "TOKEN_FAILED"); return
    try:
        role = conductor_core.topic_role(cfg, run.node)
    except conductor_core.UnknownTopic as e:
        _escalate(run, "ERROR", f"unknown topic {e}"); return
    target = run.arch if role == "Architect" else run.dev

    import uuid
    dispatch_id = uuid.uuid4().hex

    # Requirement H-7 / H-8 Prompt Augmentation
    def augment(base_text):
        hook_path = _config.get('Hook')
        if not hook_path:
            return base_text

        cmd_parts = [
            hook_path,
            "--prompt",
            target,
            "Stop",
            dispatch_id,
            str(Path(run.cwd).resolve())
        ]
        hook_cmd = " ".join(shlex.quote(p) for p in cmd_parts)
        hns = _config.get('HumanNotifyScript')

        event_sentence = (
            f"When the task is finished, run this exact command once as your final action "
            f"and verify its stdout contains EVENT_SENT_OK:\n"
            f"```bash\n{hook_cmd}\n```\n"
            f"{f'If the command does not print EVENT_SENT_OK, run this script to notify the human: {hns} {target} \"{run.node}\" and then stop. ' if hns else ''}"
            f"Do not modify or truncate the command path."
        )

        notify_sentence = ""
        if hns:
            # Deterministic at endTopic
            if run.node == _flow_config.get('endTopic'):
                notify_sentence = f"Also run this script to notify the human: {hns} {target} \"{run.node}\""
            else:
                # Conditional on escalatable topics
                qe_topics = set(_flow_config.get('questionsEscalateTopics', []))
                entry = _flow_config.get('topics', {}).get(run.node, {})
                if run.node in qe_topics or entry.get('escalateKind') == 'QUESTION':
                    notify_sentence = f"If you raise a [HUMAN] question this turn, also run this script: {hns} {target} \"{run.node}\""

        augmented = base_text.strip() + "\n\n" + event_sentence
        if notify_sentence:
            augmented += "\n\n" + notify_sentence
        return augmented

    # Resolve the base message: the human answer (override) or the topic text.
    if message_override:
        base_msg = message_override
    else:
        try:
            base_msg = conductor_core.topic_message(cfg, run.node)
        except Exception as e:
            _escalate(run, "ERROR", f"failed to resolve topic message: {e}"); return

    # Bundle the once-per-role Pre-Prompt (collaboration policy) into this role's
    # FIRST workflow turn instead of sending it as a separate contentless turn.
    # A standalone policy turn relies on the agent independently firing a hook
    # event to acknowledge it (observed to fail with some agents); bundling makes
    # the appended completion line apply to a real task and removes that stall.
    bundled_pp = False
    if message_override is None and not run.roles_initialized.get(role, False):
        pre_prompt = _get_pre_prompt_text(cfg)
        if pre_prompt:
            base_msg = pre_prompt + "\n\n" + base_msg
            bundled_pp = True
        run.roles_initialized[role] = True

    suffix = (" (Override)" if message_override else "") + (" +Pre-Prompt" if bundled_pp else "")
    log_decision(run, f"{'Dispatching':<{_LOG_VERB_W}}{run.node}  to {target}{suffix}")

    final_msg = augment(base_msg)
    event_file = get_event_file(run.cwd) if _config else None
    offset = event_file.stat().st_size if event_file and event_file.exists() else 0
    run.dispatch_marker = {"ts": utc_now_iso(), "offset": offset, "target": target, "dispatch_id": dispatch_id}
    run.watermark = offset
    run.status = "DISPATCHED_AWAITING_EVENT" if not message_override else "HUMAN_ANSWER_SENT_AWAITING_EVENT"
    run.save()
    try:
        r = conductor_core.dispatch(cfg, run.project, target, run.node, final_msg)
        if not r.ok:
            _escalate(run, "ERROR", f"dispatch failed: {r.error}")
    except Exception as e:
        _escalate(run, "ERROR", f"dispatch failed: {e}")


# ── Event tailing ─────────────────────────────────────────────────────────────

def check_for_event(run):
    expected_did = run.dispatch_marker.get("dispatch_id")
    if not expected_did:
        _escalate(run, "ERROR", "Legacy in-flight run missing dispatch_id. Please resume to redispatch.")
        return

    event_file = get_event_file(run.cwd)
    if not event_file.exists(): return
    with open(event_file, 'r', encoding='utf-8') as f:
        f.seek(run.watermark); lines = f.readlines(); new_watermark = f.tell()
    matched = False
    for line in lines:
        try:
            evt = json.loads(line)
            evt_cwd_raw = evt.get("cwd")
            if not evt_cwd_raw: continue
            evt_cwd = str(Path(evt_cwd_raw).resolve())
            run_cwd = str(Path(run.cwd).resolve())
            if (evt.get("dispatch_id") == expected_did and
                    evt_cwd == run_cwd and evt.get("target") == run.dispatch_marker["target"]
                    and evt.get("event") in ["Stop", "AfterAgent"]):
                matched = True
        except Exception: continue
        if matched: break
    run.watermark = new_watermark
    if not matched:
        run.save()
        return

    run.total_turns += 1
    run.dispatch_marker["event_matched_at"] = utc_now_iso()
    run.status = "IN TRANSITION TO ..."

    needs_delay = False
    if _flow_config:
        entry = _flow_config.get('topics', {}).get(run.node, {})
        q_sentinel = _flow_config.get('questionsSentinel', 'TempTBD_Questions.md')
        questions_back = _flow_config.get('questionsBackTo')
        questions_escalate = set(_flow_config.get('questionsEscalateTopics', []))

        topic_declares_questions = (entry.get('escalateIf') == q_sentinel or entry.get('backIf') == q_sentinel)
        has_universal_guard = (
            ((questions_back and run.node != questions_back) or run.node in questions_escalate)
            and not topic_declares_questions
        )

        if entry.get('backIf') or entry.get('nextIf') or entry.get('escalateIf') or has_universal_guard:
            needs_delay = True

    initial_wait_sec = get_dispatch_event_prompt_config().get('initialWaitSec', 10)

    if needs_delay and initial_wait_sec > 0 and run.total_turns > 1:
        due = (datetime.now(timezone.utc) + timedelta(seconds=initial_wait_sec)).isoformat().replace("+00:00", "Z")
        run.dispatch_marker["transition_due_at"] = due
    else:
        run.dispatch_marker.pop("transition_due_at", None)

    # Persist event consumption and recoverable transition state together.
    run.save()
    log_decision(run, f"{'Turn complete':<{_LOG_VERB_W}}{run.node}  (turn {run.total_turns})")

    if not (needs_delay and initial_wait_sec > 0 and run.total_turns > 1):
        transition(run)

def check_timeout(run):
    if run.status == "IN TRANSITION TO ...": return
    ts_str = run.dispatch_marker.get("ts")
    if not ts_str: return
    try:
        elapsed = (datetime.now(timezone.utc) - parse_iso_timestamp(ts_str).astimezone(timezone.utc)).total_seconds()
    except Exception: return
    timeout_sec = (_config.get('limits', {}).get('turnTimeoutSec', 1200) if _config else 1200)
    if elapsed > timeout_sec:
        log_decision(run, f"TURN_TIMEOUT: waited {elapsed:.0f}s, limit {timeout_sec}s")
        run.status = "PAUSED_FOR_HUMAN"; run.escalation_kind = "TURN_TIMEOUT"
        notify_operator(run, "TURN_TIMEOUT"); run.save()

def fire_scheduled_run(run):
    """Dispatch a SCHEDULED run once its scheduled_for instant has arrived.
    Re-checks required files (they may have changed since scheduling) and
    escalates to the human rather than dispatching into a broken state."""
    due = run.scheduled_for
    if not due: return
    try:
        if datetime.now(timezone.utc) < parse_iso_timestamp(due).astimezone(timezone.utc):
            return
    except Exception:
        return
    missing = missing_required_files(run.cwd)
    if missing:
        run.scheduled_for = None
        _escalate(run, "ERROR", f"scheduled start aborted — required file missing: {missing}")
        return
    run.scheduled_for = None
    run.status = "RUNNING"
    log_decision(run, "Scheduled time reached — starting run")
    dispatch(run)

def fire_scheduled_resume(run):
    """Fire a PAUSED_BY_USER resume whose resume_at instant has arrived."""
    due = run.resume_at
    if not due: return
    try:
        if datetime.now(timezone.utc) < parse_iso_timestamp(due).astimezone(timezone.utc):
            return
    except Exception:
        return
    send_cmd = run.resume_send_command
    run.resume_at = None
    run.resume_send_command = None
    if send_cmd:
        target = run.dispatch_marker.get('target', '')
        session = f"{target}_{run.project}"
        try:
            result = subprocess.run(
                ['tmux', 'send-keys', '-t', session, send_cmd, 'Enter'],
                capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                _escalate(run, "ERROR", f"scheduled resume tmux send failed (session '{session}'): {result.stderr.strip()}")
                return
            log_decision(run, f"Sent to {session}: {send_cmd}")
        except Exception as e:
            _escalate(run, "ERROR", f"scheduled resume tmux send error: {e}")
            return
    log_decision(run, "Scheduled resume time reached — resuming run")
    run.status = "RUNNING"; run.save()
    dispatch(run)

def poll_runs():
    with _lock: runs = list(_run_states.values())
    for run in runs:
        if run.status == "SCHEDULED":
            fire_scheduled_run(run)
        elif run.status == "PAUSED_BY_USER" and run.resume_at:
            with _lock: fire_scheduled_resume(run)
        elif "AWAITING_EVENT" in run.status:
            check_for_event(run)
            if "AWAITING_EVENT" in run.status:  # still waiting — check timeout
                check_timeout(run)
        elif run.status == "IN TRANSITION TO ...":
            due = run.dispatch_marker.get("transition_due_at")
            should_transition = False
            if due:
                try:
                    due_dt = parse_iso_timestamp(due).astimezone(timezone.utc)
                    if datetime.now(timezone.utc) >= due_dt:
                        should_transition = True
                except Exception: pass
            else:
                # Interrupted transition or manual skip-ahead
                should_transition = True

            if should_transition:
                run.dispatch_marker.pop("transition_due_at", None)
                run.save()  # Persist consumption before side effects
                transition(run)

def tail_events():
    while True:
        poll_runs()
        time.sleep(_config.get('limits', {}).get('tailPollSec', 5) if _config else 5)


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route('/')
def index(): return render_template('index.html')

@app.route('/logo.png')
def logo():
    static_dir = Path(__file__).resolve().parent / 'static'
    return send_from_directory(str(static_dir), 'LLMDirector.png')

@app.route('/api/runs', methods=['GET'])
def list_runs():
    result = []
    for r in _run_states.values():
        d = r.to_dict()
        d['progress'] = get_run_progress(r)
        result.append(d)
    return jsonify(result)

@app.route('/api/config', methods=['GET'])
def get_config_route(): return jsonify(_config)

@app.route('/api/conductor-config', methods=['GET'])
def get_conductor_config_route():
    try:
        cfg = get_conductor_config()
        resp = dict(cfg) if isinstance(cfg, dict) else cfg
        if _flow_config_error:
            resp['flowConfigError'] = _flow_config_error
        elif _flow_config:
            resp['flowStartTopic'] = _flow_config['startTopic']
            resp['flowEntryTopics'] = entry_topics_ordered(_flow_config)
        return jsonify(resp)
    except Exception as e:
        # Always return 200 so loadDiscovery() can parse the body and render the error panel
        return jsonify({"error": str(e), "flowConfigError": _flow_config_error or str(e)})

@app.route('/api/pause', methods=['POST'])
def pause_run():
    cwd = request.get_json().get('cwd')
    with _lock:
        if cwd in _run_states:
            _run_states[cwd].status = "PAUSED_BY_USER"; _run_states[cwd].save()
            return jsonify({"ok": True})
    return jsonify({"error": "Run not found"}), 404

@app.route('/api/resume', methods=['POST'])
def resume_run():
    body = request.get_json() or {}
    cwd = body.get('cwd')
    with _lock:
        if cwd in _run_states:
            run = _run_states[cwd]
            if run.status in ("PAUSED_BY_USER", "PAUSED_FOR_HUMAN"):

                # Clear scheduled resume — only valid for PAUSED_BY_USER
                if run.status == "PAUSED_BY_USER" and body.get('resumeMode') == 'clear':
                    run.resume_at = None
                    run.resume_send_command = None
                    log_decision(run, "Scheduled resume cleared by human")
                    run.save()
                    return jsonify({"ok": True})

                # Scheduled resume — only valid for PAUSED_BY_USER
                if run.status == "PAUSED_BY_USER" and body.get('resumeMode') == 'scheduled':
                    try:
                        resume_at = next_local_time_utc(int(body['scheduleHour']),
                                                        int(body['scheduleMinute']))
                    except Exception:
                        return jsonify({"error": "Invalid scheduled time"}), 400
                    run.resume_at = resume_at
                    run.resume_send_command = (body.get('sendCommand') or '').strip() or None
                    log_decision(run, f"Resume scheduled for {fmt_ts(parse_iso_timestamp(resume_at))}"
                                 + (f" with send: {run.resume_send_command}" if run.resume_send_command else ""))
                    run.save()
                    return jsonify({"ok": True})

                # Pre-resume tmux command — only honoured for PAUSED_BY_USER
                if run.status == "PAUSED_BY_USER":
                    send_cmd = (body.get('sendCommand') or '').strip()
                    if send_cmd:
                        target = run.dispatch_marker.get('target', '')
                        session = f"{target}_{run.project}"
                        try:
                            result = subprocess.run(
                                ['tmux', 'send-keys', '-t', session, send_cmd, 'Enter'],
                                capture_output=True, text=True, timeout=5)
                            if result.returncode != 0:
                                return jsonify({"error": f"tmux send-keys failed (session '{session}'): {result.stderr.strip()}"}), 400
                            log_decision(run, f"Sent to {session}: {send_cmd}")
                        except Exception as e:
                            return jsonify({"error": f"tmux send-keys error: {e}"}), 400
                if run.escalation_kind == "COMMIT_APPROVAL":
                    entry = _flow_config['topics'].get(run.node, {}) if _flow_config else {}
                    nxt = entry.get('nextTo') if entry.get('action') == 'commit_approval' else None
                    if nxt:
                        # Approval gate, then continue: dispatch the configured
                        # follow-up node (e.g. the actual commit turn).
                        run.escalation_kind = None
                        run.node = nxt
                        run.status = "RUNNING"; run.save()
                        log_decision(run, "Commit approved by human — proceeding to " + nxt)
                        dispatch(run); return jsonify({"ok": True})
                    # No follow-up node configured → approval ends the run.
                    run.status = "DONE"
                    log_decision(run, "Commit approved by human — run DONE")
                    release_if_last_run(run); run.save()
                    return jsonify({"ok": True})
                if run.escalation_kind == "TURN_TIMEOUT" and run.pending_workflow_node:
                    # Pre-prompt timed out — retry by re-dispatching (role not yet initialized)
                    run.pending_workflow_node = None
                    run.status = "RUNNING"; run.save()
                    dispatch(run); return jsonify({"ok": True})

                if run.escalation_kind == "TURN_TIMEOUT":
                    # Agent is still working — just extend the wait window without re-dispatching
                    run.dispatch_marker["ts"] = utc_now_iso()
                    run.status = "DISPATCHED_AWAITING_EVENT"; run.escalation_kind = None
                    run.save(); return jsonify({"ok": True})

                # For escalations that fired before routing to the next node, the old
                # dispatch_marker belongs to the already-processed turn — re-entering
                # DISPATCHED_AWAITING_EVENT would stall waiting for an event that will
                # never arrive. Force a fresh dispatch for all these cases.
                # Stagnation: only the unchanged sentinel file blocks progress.
                # If it is already gone, advance the FSM silently. If it still
                # exists, require explicit human permission to delete it before
                # advancing; absent that, stay paused (the human re-clicks Resume).
                if run.escalation_kind == "STAGNATION":
                    cwd_path = Path(run.cwd)
                    existing = [s for s in sorted(node_sentinels(run))
                                if (cwd_path / s).exists()]
                    if existing and not body.get('delete'):
                        return jsonify({"needs_confirmation": True, "files": existing})
                    for s in existing:
                        try: (cwd_path / s).unlink()
                        except Exception: pass
                    if existing:
                        log_decision(run, "Stagnation sentinel(s) deleted by human: "
                                     + ", ".join(existing))
                    run.stagnation_hashes.pop(run.node, None)
                    run.status = "RUNNING"; run.save()
                    transition(run); return jsonify({"ok": True})

                if run.escalation_kind in ("LOOP_CAP", "MAX_TURNS",
                                           "ERROR", "TOKEN_FAILED"):
                    if run.escalation_kind == "MAX_TURNS":
                        run.total_turns = 0
                    if run.escalation_kind in ("LOOP_CAP", "MAX_TURNS"):
                        for k in run.counters: run.counters[k] = 0
                    run.status = "RUNNING"; run.save()
                    dispatch(run); return jsonify({"ok": True})

                # PAUSED_BY_USER or QUESTION-already-answered: re-tail or dispatch
                if run.dispatch_marker.get("transition_due_at"):
                    run.status = "IN TRANSITION TO ..."
                else:
                    run.status = "DISPATCHED_AWAITING_EVENT" if run.dispatch_marker.get("ts") else "RUNNING"

                if run.status == "RUNNING": dispatch(run)
                run.save(); return jsonify({"ok": True})
    return jsonify({"error": "Run not resumable"}), 400

@app.route('/api/answer', methods=['POST'])
def answer_run():
    data = request.get_json(); cwd = data.get('cwd'); answer = data.get('answer')
    with _lock:
        if cwd in _run_states:
            run = _run_states[cwd]
            if run.status == "PAUSED_FOR_HUMAN":
                if run.escalation_kind != "QUESTION":
                    return jsonify({"error": "Answer only valid for QUESTION escalation"}), 400
                # Compose resumed prompt: original topic Mesg + human answer
                try:
                    cfg = get_conductor_config()
                    mesg_parts = cfg.get(run.node, {}).get('Mesg', [])
                    original_mesg = "\n\n".join(mesg_parts) if isinstance(mesg_parts, list) else str(mesg_parts)
                except Exception:
                    original_mesg = "(original prompt unavailable)"
                q_sentinel = (_flow_config.get('questionsSentinel', 'TempTBD_Questions.md')
                              if _flow_config else 'TempTBD_Questions.md')
                composed = (
                    "Continue the current LLMDirector topic using the original topic "
                    "instructions and the human answer below.\n\n"
                    "Original topic instructions:\n" + original_mesg + "\n\n"
                    f"Human response to the pending {q_sentinel} questions:\n" + answer + "\n\n"
                    "Apply the human response to the current topic. Update or delete the relevant "
                    "TempTBD_*.md files according to the original topic instructions, then finish "
                    "your turn normally."
                )
                dispatch(run, message_override=composed)
                return jsonify({"ok": True})
    return jsonify({"error": "Run not in escalation"}), 400

@app.route('/api/abort', methods=['POST'])
def abort_run():
    cwd = request.get_json().get('cwd')
    with _lock:
        if cwd in _run_states:
            run = _run_states[cwd]
            run.status = "ABORTED"
            log_decision(run, "Run aborted by operator")
            run.save()
            _run_states.pop(cwd)
            release_if_last_run(run)
            return jsonify({"ok": True})
    return jsonify({"error": "Run not found"}), 404

@app.route('/api/conclude', methods=['POST'])
def conclude_run():
    """Dismiss a terminal (DONE/ABORTED/ERROR) run from the dashboard."""
    cwd = request.get_json().get('cwd')
    with _lock:
        if cwd in _run_states:
            run = _run_states[cwd]
            if run.status not in ("DONE", "ABORTED", "ERROR"):
                return jsonify({"error": "Run is not in a terminal state"}), 400
            _run_states.pop(cwd)
            return jsonify({"ok": True})
    return jsonify({"error": "Run not found"}), 404

@app.route('/api/tmux', methods=['GET'])
def get_tmux():
    prj = request.args.get('project'); targ = request.args.get('target')
    session = f"{targ}_{prj}"
    try:
        res = subprocess.run(['tmux', 'capture-pane', '-t', session, '-p'],
                             capture_output=True, text=True, check=True)
        return jsonify({"output": res.stdout})
    except Exception:
        try: return jsonify({"output": conductor_core.capture_pane(prj, targ)})
        except Exception as e: return jsonify({"error": str(e)}), 502

@app.route('/api/md', methods=['GET'])
def get_md():
    cwd = request.args.get('cwd'); file = request.args.get('file')
    p = Path(cwd) / file
    if not p.exists(): return jsonify({"error": "File not found"}), 404
    return p.read_text(encoding='utf-8'), 200, {'Content-Type': 'text/plain'}

@app.route('/api/start', methods=['POST'])
def start_run():
    if _flow_config_error:
        return jsonify({"error": f"Flow config invalid — cannot start run: {_flow_config_error}"}), 503
    data = request.get_json(); project = data.get('project')
    arch = data.get('arch', 'CLAUDE'); dev = data.get('dev', 'CODEX')
    if not project: return jsonify({"error": "Missing project"}), 400
    if arch == dev: return jsonify({"error": "Architect and Developer must be different targets"}), 400

    # Per-run start topic: must be one of the curated entry points (defaults to s0).
    start_topic = data.get('startTopic')
    allowed_entries = entry_topics_ordered(_flow_config) if _flow_config else []
    if start_topic:
        if start_topic not in allowed_entries:
            return jsonify({"error": f"Invalid start topic '{start_topic}'; "
                                     f"choose one of: {allowed_entries}"}), 400
        effective_start = start_topic
    else:
        effective_start = _flow_config['startTopic'] if _flow_config else None
    try:
        res_arch = subprocess.run(['tmux','display','-p','-t',arch+"_"+project,'#{pane_current_path}'],
                                  capture_output=True, text=True, check=True)
        res_dev  = subprocess.run(['tmux','display','-p','-t',dev+"_"+project,'#{pane_current_path}'],
                                  capture_output=True, text=True, check=True)
        cwd_arch = res_arch.stdout.strip(); cwd_dev = res_dev.stdout.strip()
        if cwd_arch != cwd_dev: return jsonify({"error": "CWD mismatch"}), 400
        cwd = cwd_arch
    except subprocess.CalledProcessError: return jsonify({"error": "Discovery failed"}), 400

    cwd_path = Path(cwd)
    missing = missing_required_files(cwd_path)
    if missing:
        return jsonify({"error": f"Required file missing: {missing}"}), 400

    try: preflight_hooks(cwd, arch, dev)
    except Exception as e: return jsonify({"error": str(e)}), 400

    # Optional deferred start: "scheduled" defers dispatch until scheduled_for.
    scheduled_for = None
    if data.get('startMode') == 'scheduled':
        try:
            scheduled_for = next_local_time_utc(int(data.get('scheduleHour')),
                                                int(data.get('scheduleMinute')))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid scheduled time"}), 400

    with _lock:
        if cwd in _run_states: return jsonify({"error": "Run active"}), 409
        # Resume a persisted run only if it is still live; a terminal saved state
        # (DONE/ABORTED/ERROR) yields to a fresh run from the chosen start topic.
        existing = RunState.load(cwd)
        if existing and existing.status not in ("DONE", "ABORTED", "ERROR"):
            run = existing
        else:
            run = RunState(project, cwd, arch, dev, start_node=effective_start)
        if scheduled_for and run is not existing:
            run.status = "SCHEDULED"; run.scheduled_for = scheduled_for
        _run_states[cwd] = run
    if run.status == "SCHEDULED":
        run.save()
        log_decision(run, f"Run scheduled for {fmt_ts(parse_iso_timestamp(run.scheduled_for))}")
    elif run.status == "RUNNING": dispatch(run)
    elif "AWAITING_EVENT" in run.status or run.status == "IN TRANSITION TO ...": log_decision(run, "Resuming run")
    return jsonify({"ok": True, "cwd": cwd})


# ── Startup ───────────────────────────────────────────────────────────────────

def reload_scheduled_runs():
    """Repopulate _run_states with persisted live runs after a Director restart.

    SCHEDULED runs survive as before; in-flight runs also re-enter the poll loop
    so a service restart does not silently drop an active workflow from the
    dashboard.
    """
    if not _config: return
    log_dir = Path(_config['logDir'])
    if not log_dir.is_dir(): return
    for state_file in log_dir.glob("*.state.json"):
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        cwd = data.get('cwd')
        run = RunState.load(cwd) if cwd else None
        if run and run.status not in ("DONE", "ABORTED", "ERROR"):
            _run_states[cwd] = run
            if run.status == "SCHEDULED":
                print(f"Reloaded scheduled run: {run.project} ({cwd}) for {run.scheduled_for}")
            else:
                print(f"Reloaded live run: {run.project} ({cwd}) status={run.status}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='LLMDirector.json')
    args = parser.parse_args(); load_config(args.config)
    load_flow_config()
    if _flow_config_error:
        print(f"WARNING: Flow config validation failed:\n{_flow_config_error}", file=sys.stderr)
        print("Director will start but run creation is blocked until config is fixed.", file=sys.stderr)
    else:
        print(f"Flow config loaded: {len(_flow_config.get('topics',{}))} topics, "
              f"start={_flow_config['startTopic']}, end={_flow_config['endTopic']}")
    reload_scheduled_runs()
    threading.Thread(target=tail_events, daemon=True).start()
    port = _config.get('serverPort', 8081) if _config else 8081
    hook = _config.get("Hook", "") if _config else ""
    if hook:
        try: _deploy_hook_script(hook)
        except Exception as e: print(f"WARNING: hook script deploy failed: {e}", file=sys.stderr)
    app.run(host='0.0.0.0', port=port, debug=False)
