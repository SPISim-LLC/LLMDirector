#!/usr/bin/env python3
"""realworld_setup.py - Real-world (live-LLM) end-to-end test harness for LLMDirector.

Unlike the dry-run (dryrun_setup.py + dryrun_agent.sh), which puts a *fake*
auto-responding shell script in each tmux session, this harness leaves the two
tmux sessions empty so YOU can attach and launch a *real* LLM CLI (claude /
codex / gemini) in each. The Director then drives those live agents through the
full flow.

To keep token cost negligible, every topic prompt is overridden with a 1-3 line
instruction (in a sandbox LLMConductor.json) that just creates/deletes the right
TempTBD_* sentinel files so the Director's routing branches are exercised. The
Director auto-appends the `--prompt` completion line (script mode), so each agent
emits the turn-end event by running the deployed LLMHookEvent.sh.

Scenario (touches every node 02-11 once, plus one QUESTION escalation and the
commit-approval gate):

  02 raise question -> 03 (escalates QUESTION; you answer in dashboard, telling it
  to delete the question) -> 02 clean -> 04 -> 05 (stub PASS) -> 06 findings ->
  07 review-question -> 08 clears it -> 09 -> 05 (PASS) -> 06 clean -> 10 ->
  11 (COMMIT_APPROVAL; you approve in dashboard) -> DONE.

Usage:
  xta/bin/realworld_setup.py              # build sandbox + empty tmux sessions
  xta/bin/realworld_setup.py --teardown   # kill sessions + remove sandbox
"""
import os
import json
import shutil
import subprocess
import argparse
from pathlib import Path

# --- REAL-WORLD TEST CONSTANTS ---
RT_PROJECT = "REALTEST"
RT_ARCH    = "ARCH_RT"
RT_DEV     = "DEV_RT"
RT_PORT    = 58083
RT_DIR     = Path("/tmp/llmd_realtest")
# ---------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
REAL_LLMD_CONFIG = ROOT_DIR / "LLMDirector.json"
REAL_LLMC_CONFIG = ROOT_DIR / "LLMConductor" / "LLMConductor.json"

# Cheap per-topic prompt overrides (replace each topic's "Mesg"). Flag files
# (.rt_spec, .rt_review) make the loop branches deterministic without per-turn
# state in the prompt. All file operations are in the current directory.
TOPIC_MESG = {
    "02: Critique_Spec": [
        "Test step 02.",
        "If file .rt_spec exists here: delete TempTBD_Questions.md if present, then reply 'spec ok'.",
        "Otherwise: create file .rt_spec, write the word q into TempTBD_Questions.md, then reply 'questions raised'.",
    ],
    "03: Answer_Update_Spec": [
        "Test step 03.",
        "Leave TempTBD_Questions.md as-is; do NOT delete it. Reply 'sending to human'.",
    ],
    "04: Implement_Spec": [
        "Test step 04. Reply 'implemented'. Do NOT create TempTBD_Questions.md or any other file.",
    ],
    "05: Validate_Implementation": [
        "Test step 05. Reply 'tests run'. Do not create or delete any file.",
    ],
    "06: Review_Implementation": [
        "Test step 06.",
        "If file .rt_review exists here: delete TempTBD_Review.md if present, then reply 'clean'.",
        "Otherwise: create file .rt_review, write the word finding into TempTBD_Review.md, then reply 'findings'.",
    ],
    "07: Critique_Review": [
        "Test step 07. Write the word rq into TempTBD_ReviewQuestions.md, then reply 'critique'.",
    ],
    "08: Clarify_Review": [
        "Test step 08. Delete TempTBD_ReviewQuestions.md, then reply 'clarified'. Do not create any file.",
    ],
    "09: Address_Review": [
        "Test step 09. Delete TempTBD_Review.md and TempTBD_ReviewQuestions.md if present, then reply 'addressed'.",
    ],
    "10: Update_Docs_And_Goldens": [
        "Test step 10. Reply 'docs updated'. Do not create or delete any file.",
    ],
    "11: Ready_To_Commit": [
        "Test step 11. Reply 'ready to commit'. Do not create or delete any file.",
    ],
}

# One-line Pre-Prompt (sent once per role) - keep it tiny.
PRE_PROMPT = [
    "You are in an automated test. Follow each instruction literally and briefly. "
    "Do not ask questions or add commentary."
]


def teardown():
    print(f"Cleaning up real-world sandbox in {RT_DIR}...")
    print(f"  Stopping Director server on port {RT_PORT}")
    pids = subprocess.run(["lsof", "-ti", f":{RT_PORT}"],
                          capture_output=True, text=True).stdout.split()
    for pid in pids:
        subprocess.run(["kill", pid], stderr=subprocess.DEVNULL)
    for role in [RT_ARCH, RT_DEV]:
        session = f"{role}_{RT_PROJECT}"
        print(f"  Killing tmux session: {session}")
        subprocess.run(["tmux", "kill-session", "-t", session], stderr=subprocess.DEVNULL)
    if RT_DIR.exists():
        shutil.rmtree(RT_DIR)
    print("Teardown complete.")


def setup():
    print(f"Setting up real-world sandbox in {RT_DIR}...")

    # 1. Sandbox dirs
    if RT_DIR.exists():
        shutil.rmtree(RT_DIR)
    RT_DIR.mkdir(parents=True)
    (RT_DIR / "logs").mkdir()
    (RT_DIR / "events").mkdir()
    proj_dir = RT_DIR / "project"
    proj_dir.mkdir()

    # 2. Sandbox LLMConductor.json: add project/targets + cheap-prompt overrides.
    with open(REAL_LLMC_CONFIG) as f:
        llmc = json.load(f)
    # Replace (not append) so the dashboard's New Run dropdowns only offer the
    # test project/targets — the operator's real projects/targets are hidden.
    llmc["Project"] = [RT_PROJECT]
    llmc["Target"] = [RT_ARCH, RT_DEV]
    # Tiny Pre-Prompt
    llmc.setdefault("Default", {})["Pre-Prompt"] = PRE_PROMPT
    # Override each topic's Mesg with the cheap prompt (keep Role/Desc intact).
    for topic, mesg in TOPIC_MESG.items():
        if topic in llmc and isinstance(llmc[topic], dict):
            llmc[topic]["Mesg"] = mesg
    sandbox_llmc_path = RT_DIR / "LLMConductor.json"
    with open(sandbox_llmc_path, "w") as f:
        json.dump(llmc, f, indent=2)

    # 3. Sandbox LLMDirector.json: sandbox paths, isolated script-mode hook.
    with open(REAL_LLMD_CONFIG) as f:
        llmd = json.load(f)
    llmd["conductorJsonPath"] = str(sandbox_llmc_path)
    llmd["eventDir"] = str(RT_DIR / "events")
    llmd["logDir"] = str(RT_DIR / "logs")
    llmd["serverPort"] = RT_PORT
    # Isolated hook so we never clobber the operator's ~/batch/LLMHookEvent.sh.
    llmd["Hook"] = str(RT_DIR / "LLMHookEvent.sh")
    # Silence human-notify side effects during the test (no RGB/Pushover/Gotify).
    llmd["HumanNotifyScript"] = "/bin/echo"
    real_flow = ROOT_DIR / "LLMDirector_Flow.json"
    sandbox_flow = RT_DIR / "LLMDirector_Flow.json"
    shutil.copy(real_flow, sandbox_flow)
    llmd["directorFlowJsonPath"] = str(sandbox_flow)
    sandbox_llmd_path = RT_DIR / "LLMDirector.json"
    with open(sandbox_llmd_path, "w") as f:
        json.dump(llmd, f, indent=2)

    # 4. Project files: required docs + passing validate stub.
    (proj_dir / "xta" / "tst").mkdir(parents=True)
    shutil.copy(ROOT_DIR / "xta" / "tst" / "dryrun_RunTest.py",
                proj_dir / "xta" / "tst" / "RunTest.py")
    (proj_dir / "Task_FSD.md").write_text("# Real-world test FSD\n")
    (proj_dir / "Task_CodeReview.md").write_text("# Real-world test Review\n")

    # 5. Empty tmux sessions (a shell cd'd into the project) - YOU launch the LLM.
    for role in [RT_ARCH, RT_DEV]:
        session = f"{role}_{RT_PROJECT}"
        subprocess.run(["tmux", "kill-session", "-t", session], stderr=subprocess.DEVNULL)
        print(f"  Starting empty tmux session: {session}")
        subprocess.run(["tmux", "new-session", "-d", "-s", session, "-c", str(proj_dir)])

    arch_session = f"{RT_ARCH}_{RT_PROJECT}"
    dev_session = f"{RT_DEV}_{RT_PROJECT}"

    print("\n" + "=" * 64)
    print(" REAL-WORLD SANDBOX READY (live LLMs)")
    print("=" * 64)
    print(f" Project : {RT_PROJECT}   Architect: {RT_ARCH}   Developer: {RT_DEV}")
    print(f" Port    : {RT_PORT}      Project dir: {proj_dir}")
    print("-" * 64)
    print(" 1. ATTACH TO EACH SESSION AND LAUNCH A REAL LLM (claude / codex / gemini):")
    print(f"      tmux attach -t {arch_session}     # then launch the Architect LLM")
    print(f"      tmux attach -t {dev_session}     # then launch the Developer LLM")
    print("    The sessions already start in the project dir. Launch the LLM in a")
    print("    mode that lets it run bash WITHOUT a manual approval each turn -")
    print("    otherwise the appended LLMHookEvent.sh line won't fire and the run stalls.")
    print("    Detach with Ctrl-b d.")
    print("\n 2. START THE DIRECTOR (the leading 'cd' is required):")
    print(f"      cd {ROOT_DIR} && python3 web/app.py --config {sandbox_llmd_path}")
    print(f"\n 3. OPEN THE DASHBOARD:  http://localhost:{RT_PORT}")
    print("\n 4. NEW RUN -> select:")
    print(f"      Project={RT_PROJECT}  Architect={RT_ARCH}  Developer={RT_DEV}")
    print("\n 5. THE RUN PAUSES FOR YOU:")
    print("      * QUESTION (node '03: Answer_Update_Spec'): click ANSWER and type")
    print("          Delete TempTBD_Questions.md then reply done")
    print("        then SEND ANSWER. (The Architect agent must delete the file for the")
    print("        flow to advance.)")
    print("      * If the agent ignores that and the run pauses with STAGNATION, the")
    print("        card names the file to delete — e.g.:")
    print(f"          rm {proj_dir}/TempTBD_Questions.md")
    print("        then click RESUME.")
    print("      * COMMIT_APPROVAL (node '11: Ready_To_Commit'): click APPROVE COMMIT")
    print("        to finish the run (-> DONE).")
    print("-" * 64)
    print(" WHEN DONE, TEAR DOWN (stops Director, kills sessions, removes sandbox):")
    print(f"      {Path(__file__).resolve()} --teardown")
    print("=" * 64 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Real-world live-LLM test harness")
    parser.add_argument("-t", "--teardown", action="store_true",
                        help="Kill sessions and remove sandbox directory")
    args = parser.parse_args()
    if args.teardown:
        teardown()
    else:
        setup()
