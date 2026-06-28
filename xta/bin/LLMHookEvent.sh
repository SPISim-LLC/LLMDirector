#!/bin/bash
# LLMDirector Hook Event Normalizer
# Positional syntax: <Hook path> --prompt <TARGET> <EVENT> <DISPATCH_ID> <CANONICAL_CWD>
EVENT_DIR=""
[[ -z "$EVENT_DIR" ]] && EVENT_DIR="$HOME/.llmdirector/events"
mkdir -p "$EVENT_DIR"

if [[ $# -ne 5 || "$1" != "--prompt" ]]; then
    echo "EVENT_SEND_FAILED"
    exit 1
fi
shift

TARGET=$1
EVENT=$2
DISPATCH_ID=$3
CWD=$4

if [[ -z "$TARGET" || -z "$EVENT" || -z "$DISPATCH_ID" || -z "$CWD" ]]; then
    echo "EVENT_SEND_FAILED"
    exit 1
fi

if [[ ! "$DISPATCH_ID" =~ ^[0-9a-f]{32}$ ]]; then
    echo "EVENT_SEND_FAILED"
    exit 1
fi

if [[ "$EVENT" != "Stop" && "$EVENT" != "AfterAgent" ]]; then
    echo "EVENT_SEND_FAILED"
    exit 1
fi

CANONICAL_CWD=$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$CWD" 2>/dev/null)
if [[ -z "$CANONICAL_CWD" ]]; then
    echo "EVENT_SEND_FAILED"
    exit 1
fi

CWD="$CANONICAL_CWD"
SANITIZED_CWD=$(echo "$CWD" | sed 's/^\///' | sed 's/\//_/g')
EVENT_FILE="$EVENT_DIR/$SANITIZED_CWD.ndjson"
LOCK_FILE="$EVENT_DIR/$SANITIZED_CWD.lock"

TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
export TARGET EVENT DISPATCH_ID CWD EVENT_FILE TS

(
    flock -x 200

    python3 -c '
import os, sys, json

cwd = os.environ.get("CWD", "")
target = os.environ.get("TARGET", "")
event = os.environ.get("EVENT", "")
dispatch_id = os.environ.get("DISPATCH_ID", "")
event_file = os.environ.get("EVENT_FILE", "")
ts = os.environ.get("TS", "")

def check_exists():
    if not os.path.exists(event_file):
        return False
    try:
        with open(event_file, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    if (obj.get("dispatch_id") == dispatch_id and
                        obj.get("cwd") == cwd and
                        obj.get("target") == target and
                        obj.get("event") == event):
                        return True
                except Exception:
                    pass
    except Exception:
        pass
    return False

if check_exists():
    sys.exit(0)

try:
    with open(event_file, "a", encoding="utf-8") as f:
        json.dump({
            "ts": ts,
            "cwd": cwd,
            "target": target,
            "event": event,
            "dispatch_id": dispatch_id
        }, f, separators=(",", ":"))
        f.write("\n")
except Exception:
    sys.exit(1)
'
) 200>"$LOCK_FILE"

# Verify exact event
python3 -c '
import os, sys, json
cwd = os.environ.get("CWD", "")
target = os.environ.get("TARGET", "")
event = os.environ.get("EVENT", "")
dispatch_id = os.environ.get("DISPATCH_ID", "")
event_file = os.environ.get("EVENT_FILE", "")

if not os.path.exists(event_file):
    sys.exit(1)
try:
    with open(event_file, "r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
                if (obj.get("dispatch_id") == dispatch_id and
                    obj.get("cwd") == cwd and
                    obj.get("target") == target and
                    obj.get("event") == event):
                    sys.exit(0)
            except Exception:
                pass
except Exception:
    pass
sys.exit(1)
'

if [ $? -eq 0 ]; then
    echo "EVENT_SENT_OK"
    exit 0
else
    echo "EVENT_SEND_FAILED"
    exit 1
fi
