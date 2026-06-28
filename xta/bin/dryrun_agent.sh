#!/usr/bin/env bash
# dryrun_agent.sh — Fake LLM for LLMDirector dry-run testing.
#
# Runs inside a tmux session in place of a real LLM.
# LLMConductor pastes the topic prompt here; the script detects which topic
# was dispatched by grepping the pasted text, executes the scenario step
# (create/remove sentinel files), sleeps briefly, then fires LLMHookEvent.sh
# to signal turn-end — exactly as a real agent would.
#
# Flow note (renumbered, review-before-validate):
#   01 Read_Update_Spec → 02 Critique_Spec ⇄ 03 Answer_Update_Spec → 04 Implement_Spec
#   → 05 Review_Implementation ⇄ (06 Critique_Review ⇄ 07 Clarify_Review) → 08 Address_Review → 05
#   05 (clean) → 09 Validate_Implementation [DIRECTOR-RUN, no agent turn] → 09B Judge_Validation
#   09B fail → findings → 08 Address_Review;  09B pass → 10 Update_Docs_And_Goldens
#   → 11 Prepare_To_Commit (commit approval) → 11B Stage_And_Commit_No_Push → DONE
#
# 09: Validate_Implementation is run by the Director itself (validate_report action)
# against the stubbed xta/tst/RunTest.py, so this fake agent NEVER receives that prompt.
#
# Usage:
#   exec ~/batch/dryrun_agent.sh <ROLE> <CWD> <TARGET_NAME>
#   ROLE        = ARCH or DEV
#   CWD         = absolute path to the dry-run project folder
#   TARGET_NAME = the agent name as known by Director (e.g. ARCH_LLM)

ROLE="${1:-DEV}"
CWD="${2:-$PWD}"
TARGET_NAME="${3:-$ROLE}"
STEP_DELAY=3          # seconds to "think" between receiving prompt and firing hook
READ_TIMEOUT=1.5      # seconds of silence that marks end of a paste burst

# Ensure we are in the correct directory
cd "$CWD" || exit 1

STATE_DIR="$CWD/.dryrun"
mkdir -p "$STATE_DIR"

# ── State helpers ──────────────────────────────────────────────────────────────

get_state() { local f="$STATE_DIR/$1"; [ -f "$f" ] && cat "$f" || echo 0; }
set_state() { echo "$2" > "$STATE_DIR/$1"; }
flag_set()  { touch "$STATE_DIR/$1"; }
flag_clear(){ rm -f "$STATE_DIR/$1"; }
flag_check(){ [ -f "$STATE_DIR/$1" ]; }

# ── Prompt collector ───────────────────────────────────────────────────────────
# Reads lines from stdin with a short inter-line timeout.
# Returns the full pasted block in $PROMPT_BUFFER.

collect_prompt() {
    PROMPT_BUFFER=""
    local line
    # Block until the first line arrives (the start of the paste)
    IFS= read -r line || { echo "[dryrun] stdin closed — exiting"; exit 0; }
    PROMPT_BUFFER="$line"
    # Collect remaining lines with timeout (paste finishes → silence → timeout)
    while IFS= read -r -t "$READ_TIMEOUT" line; do
        PROMPT_BUFFER="$PROMPT_BUFFER\n$line"
    done
}

# ── Topic detector ─────────────────────────────────────────────────────────────
# Sets $TOPIC to the detected topic name, or "UNKNOWN".
# Patterns key off distinctive substrings of each topic's Mesg in LLMConductor.json.

detect_topic() {
    local buf="$1"
    if   echo -e "$buf" | grep -qi "Read my answers to your questions";                       then TOPIC="01: Read_Update_Spec"
    elif echo -e "$buf" | grep -qi "Act as a cynical developer\|Critique_Spec";               then TOPIC="02: Critique_Spec"
    elif echo -e "$buf" | grep -qi "Answer the questions described in";                        then TOPIC="03: Answer_Update_Spec"
    elif echo -e "$buf" | grep -qi "as the acceptance checklist while implementing";           then TOPIC="04: Implement_Spec"
    elif echo -e "$buf" | grep -qi "Review newly updated code";                                then TOPIC="05: Review_Implementation"
    elif echo -e "$buf" | grep -qi "Before changing any code, make sure every finding";        then TOPIC="06: Critique_Review"
    elif echo -e "$buf" | grep -qi "clarifications and disagreements in updated";              then TOPIC="07: Clarify_Review"
    elif echo -e "$buf" | grep -qi "Read and then delete";                                     then TOPIC="08: Address_Review"
    elif echo -e "$buf" | grep -qi "your job is to judge the result";                          then TOPIC="09B: Judge_Validation"
    elif echo -e "$buf" | grep -qi "Refresh golden or reference\|Update documentation";        then TOPIC="10: Update_Docs_And_Goldens"
    elif echo -e "$buf" | grep -qi "Complete the LifeCycle section";                           then TOPIC="11: Prepare_To_Commit"
    elif echo -e "$buf" | grep -qi "Stage and commit all the changes";                         then TOPIC="11B: Stage_And_Commit_No_Push"
    else TOPIC="UNKNOWN"
    fi
}

# ── Hook firer ─────────────────────────────────────────────────────────────────

fire_hook() {
    local event="${1:-Stop}"
    echo "" | ~/batch/LLMHookEvent.sh "$TARGET_NAME" "$event"
    echo "[dryrun $ROLE] hook fired: $event (Target: $TARGET_NAME)"
}

# ── Scenario handlers ──────────────────────────────────────────────────────────

handle_01_Read_Update_Spec() {
    # Architect reconfirms the spec. Leave NO '<!-- LLMDIRECTOR: NO_TEST_SUITE -->'
    # marker so the Director runs the stubbed RunTest.py at the validation step.
    echo "[dryrun ARCH] Read_Update_Spec — spec confirmed, test suite applies"
    rm -f "$CWD/TempTBD_Questions.md"
}

handle_02_Critique_Spec() {
    local calls=$(( $(get_state cs_calls) + 1 ))
    set_state cs_calls "$calls"
    if [ "$calls" -le 2 ]; then
        echo "[dryrun DEV] Critique_Spec call $calls — raising questions"
        echo "Dry-run question $calls: is the endpoint path correct?" > "$CWD/TempTBD_Questions.md"
    else
        echo "[dryrun DEV] Critique_Spec call $calls — spec approved, advancing"
        rm -f "$CWD/TempTBD_Questions.md"
    fi
}

handle_03_Answer_Update_Spec() {
    echo "[dryrun ARCH] Answer_Update_Spec — answering questions"
    rm -f "$CWD/TempTBD_Questions.md"
}

handle_04_Implement_Spec() {
    echo "[dryrun DEV] Implement_Spec — clean implementation"
}

handle_05_Review_Implementation() {
    local pass=$(get_state review_pass)
    if [ "$pass" -lt 2 ]; then
        echo "[dryrun ARCH] Review_Implementation pass 1 — findings found"
        echo "Dry-run finding: error handling missing on the health endpoint." > "$CWD/TempTBD_Review.md"
        set_state review_pass 1
    else
        echo "[dryrun ARCH] Review_Implementation pass 2 — no findings, advancing to validation"
        rm -f "$CWD/TempTBD_Review.md"
    fi
}

handle_06_Critique_Review() {
    local loops=$(( $(get_state cr_loops) + 1 ))
    set_state cr_loops "$loops"
    echo "[dryrun DEV] Critique_Review loop $loops"
    # Always raise TempTBD_ReviewQuestions.md to keep the clarify loop going
    echo "Dry-run review question (loop $loops): is finding #1 still applicable?" \
        > "$CWD/TempTBD_ReviewQuestions.md"
}

handle_07_Clarify_Review() {
    local loops=$(get_state cr_loops)

    if flag_check awaiting_human; then
        # This call is the human-answer turn — clean everything up and advance
        echo "[dryrun ARCH] Clarify_Review — human answer received, finalising"
        rm -f "$CWD/TempTBD_Questions.md" "$CWD/TempTBD_ReviewQuestions.md" "$CWD/TempTBD_Review.md"
        flag_clear awaiting_human
        set_state cr_loops 0
        set_state review_pass 2   # next Review_Implementation will be clean → validation
    elif [ "$loops" -ge 3 ]; then
        # 3rd loop — escalate to human with Architect-origin TempTBD_Questions.md
        echo "[dryrun ARCH] Clarify_Review loop $loops — escalating to human"
        rm -f "$CWD/TempTBD_ReviewQuestions.md"
        echo "Dry-run human question: Please confirm the finding is resolved." \
            > "$CWD/TempTBD_Questions.md"
        flag_set awaiting_human
    else
        # Keep the loop going — leave TempTBD_ReviewQuestions.md in place → back to Critique_Review
        echo "[dryrun ARCH] Clarify_Review loop $loops — questions remain"
        # TempTBD_ReviewQuestions.md already exists from Critique_Review
    fi
}

handle_08_Address_Review() {
    echo "[dryrun DEV] Address_Review — addressed all findings (review and/or validation)"
    rm -f "$CWD/TempTBD_Review.md" "$CWD/TempTBD_ReviewQuestions.md"
}

# 09: Validate_Implementation has NO handler — the Director runs RunTest.py itself
# (validate_report action) and dispatches 09B to the Architect.

handle_09B_Judge_Validation() {
    # The Director has run the stub RunTest.py and written TempTBD_ValidationResult.md.
    # Round 1: simulate the Architect finding a real test failure → push findings back
    #          into the review file so the run loops through Address_Review.
    # Round 2: clean → delete the review file so the run advances to docs.
    local calls=$(( $(get_state jv_calls) + 1 ))
    set_state jv_calls "$calls"
    rm -f "$CWD/TempTBD_Review.md"   # clear any prior round before re-judging
    if [ "$calls" -le 1 ]; then
        echo "[dryrun ARCH] Judge_Validation round $calls — test FAIL, recording findings"
        echo "Dry-run validation finding: TestHealthEndpoint failed (regression)." > "$CWD/TempTBD_Review.md"
    else
        echo "[dryrun ARCH] Judge_Validation round $calls — validation clean, advancing"
    fi
}

handle_10_Update_Docs_And_Goldens() {
    echo "[dryrun DEV] Update_Docs_And_Goldens — clean"
}

handle_11_Prepare_To_Commit() {
    echo "[dryrun DEV] Prepare_To_Commit — Director will escalate to human for commit approval"
}

handle_11B_Stage_And_Commit_No_Push() {
    # Real agents would 'git commit' here (no push); on failure they write
    # TempTBD_CommitFailed.md so the Director escalates ERROR. The dry run simply
    # succeeds: leave no failure sentinel → Director reaches endTopic → DONE.
    echo "[dryrun DEV] Stage_And_Commit_No_Push — committed locally (no push)"
    rm -f "$CWD/TempTBD_CommitFailed.md"
}

handle_UNKNOWN() {
    # No Mesg pattern matched — could be a human-answer messageOverride dispatch
    if flag_check awaiting_human; then
        echo "[dryrun $ROLE] Received human answer (messageOverride) — passing to Clarify_Review handler"
        handle_07_Clarify_Review
    else
        echo "[dryrun $ROLE] Unrecognised prompt (ignored)"
    fi
}

# ── Main loop ──────────────────────────────────────────────────────────────────

echo "======================================"
echo " DryRun Agent — ROLE=$ROLE  CWD=$CWD "
echo " Waiting for dispatches from Director "
echo "======================================"

while true; do
    collect_prompt

    if [ -z "$PROMPT_BUFFER" ]; then
        continue
    fi

    detect_topic "$PROMPT_BUFFER"
    echo "[dryrun $ROLE] detected topic: $TOPIC"

    sleep "$STEP_DELAY"

    case "$TOPIC" in
        "01: Read_Update_Spec")         handle_01_Read_Update_Spec ;;
        "02: Critique_Spec")            handle_02_Critique_Spec ;;
        "03: Answer_Update_Spec")       handle_03_Answer_Update_Spec ;;
        "04: Implement_Spec")           handle_04_Implement_Spec ;;
        "05: Review_Implementation")    handle_05_Review_Implementation ;;
        "06: Critique_Review")          handle_06_Critique_Review ;;
        "07: Clarify_Review")           handle_07_Clarify_Review ;;
        "08: Address_Review")           handle_08_Address_Review ;;
        "09B: Judge_Validation")        handle_09B_Judge_Validation ;;
        "10: Update_Docs_And_Goldens")  handle_10_Update_Docs_And_Goldens ;;
        "11: Prepare_To_Commit")        handle_11_Prepare_To_Commit ;;
        "11B: Stage_And_Commit_No_Push")handle_11B_Stage_And_Commit_No_Push ;;
        *)                              handle_UNKNOWN ;;
    esac

    fire_hook Stop
done
