#!/usr/bin/env python3
import os
import json
import shutil
import subprocess
import argparse
from pathlib import Path

# --- DRY-RUN CONSTANTS (Configure here) ---
DRY_PROJECT = "DRYRUN"
DRY_ARCH    = "ARCH_LLM"
DRY_DEV     = "DEV_LLM"
DRY_PORT    = 58082
DRY_DIR     = Path("/tmp/llmd_dryrun")
# ------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
REAL_LLMD_CONFIG = ROOT_DIR / "LLMDirector.json"
REAL_LLMC_CONFIG = ROOT_DIR / "LLMConductor" / "LLMConductor.json"

def teardown():
    print(f"Cleaning up hermetic dry-run sandbox in {DRY_DIR}...")

    # 1. Stop the Director server bound to the dry-run port (started manually by
    #    the operator — it is not a tmux session, so kill it by port here).
    print(f"  Stopping Director server on port {DRY_PORT}")
    pids = subprocess.run(["lsof", "-ti", f":{DRY_PORT}"],
                          capture_output=True, text=True).stdout.split()
    for pid in pids:
        subprocess.run(["kill", pid], stderr=subprocess.DEVNULL)

    # 2. Kill tmux sessions
    for role in [DRY_ARCH, DRY_DEV]:
        session = f"{role}_{DRY_PROJECT}"
        print(f"  Killing tmux session: {session}")
        subprocess.run(["tmux", "kill-session", "-t", session], stderr=subprocess.DEVNULL)

    # 3. Remove directory
    if DRY_DIR.exists():
        shutil.rmtree(DRY_DIR)

    print("Teardown complete.")

def setup():
    print(f"Setting up hermetic dry-run sandbox in {DRY_DIR}...")
    
    # 1. Prepare sandbox directory
    if DRY_DIR.exists():
        shutil.rmtree(DRY_DIR)
    DRY_DIR.mkdir(parents=True)
    (DRY_DIR / "logs").mkdir()
    (DRY_DIR / "events").mkdir()
    (DRY_DIR / "project").mkdir()

    # 2. Generate Sandbox LLMConductor.json
    with open(REAL_LLMC_CONFIG, 'r') as f:
        llmc = json.load(f)
    
    # Replace (not append) so the dashboard's New Run dropdowns only offer the
    # dry-run project/targets — the operator's real projects/targets are hidden.
    llmc["Project"] = [DRY_PROJECT]
    llmc["Target"] = [DRY_ARCH, DRY_DEV]

    sandbox_llmc_path = DRY_DIR / "LLMConductor.json"
    with open(sandbox_llmc_path, 'w') as f:
        json.dump(llmc, f, indent=2)

    # 3. Generate Sandbox LLMDirector.json
    with open(REAL_LLMD_CONFIG, 'r') as f:
        llmd = json.load(f)
    
    llmd["conductorJsonPath"] = str(sandbox_llmc_path)
    llmd["eventDir"] = str(DRY_DIR / "events")
    llmd["logDir"] = str(DRY_DIR / "logs")
    llmd["serverPort"] = DRY_PORT

    # Copy companion flow config to sandbox so directorFlowJsonPath resolves correctly
    real_flow_config = ROOT_DIR / "LLMDirector_Flow.json"
    sandbox_flow_path = DRY_DIR / "LLMDirector_Flow.json"
    if real_flow_config.exists():
        shutil.copy(real_flow_config, sandbox_flow_path)
        llmd["directorFlowJsonPath"] = str(sandbox_flow_path)

    sandbox_llmd_path = DRY_DIR / "LLMDirector.json"
    with open(sandbox_llmd_path, 'w') as f:
        json.dump(llmd, f, indent=2)

    # 4. Prepare fake project files
    proj_dir = DRY_DIR / "project"
    (proj_dir / "xta" / "tst").mkdir(parents=True)
    shutil.copy(ROOT_DIR / "xta" / "tst" / "dryrun_RunTest.py", proj_dir / "xta" / "tst" / "RunTest.py")
    (proj_dir / "Task_FSD.md").write_text("# Dry Run FSD\n")
    (proj_dir / "Task_CodeReview.md").write_text("# Dry Run Review\n")

    # 5. Manage tmux sessions
    for role in [DRY_ARCH, DRY_DEV]:
        session = f"{role}_{DRY_PROJECT}"
        print(f"  Starting tmux session: {session}")
        subprocess.run(["tmux", "kill-session", "-t", session], stderr=subprocess.DEVNULL)
        
        # Start new session, run dryrun_agent.sh
        agent_script = ROOT_DIR / "xta" / "bin" / "dryrun_agent.sh"
        role_type = "ARCH" if role == DRY_ARCH else "DEV"
        cmd = f"exec {agent_script} {role_type} {proj_dir} {role}"
        subprocess.run(["tmux", "new-session", "-d", "-s", session, cmd])

    print("\n" + "="*60)
    print(" DRY-RUN SANDBOX READY")
    print("="*60)
    print(f" Project   : {DRY_PROJECT}")
    print(f" Agents    : {DRY_ARCH} & {DRY_DEV}")
    print(f" Port      : {DRY_PORT}")
    print(f" Path      : {proj_dir}")
    print("-" * 60)
    print(" TO START THE DRY-RUN:")
    print(f" 1. START THE DIRECTOR from the LLMDirector repo root ({ROOT_DIR}).")
    print("    Copy-paste this command (the leading 'cd' is required — web/app.py")
    print("    and the conductor activity log are resolved relative to that folder):")
    print(f"    cd {ROOT_DIR} && python3 web/app.py --config {sandbox_llmd_path}")
    print("\n 2. OPEN THE DASHBOARD:")
    print(f"    http://localhost:{DRY_PORT}")
    print("\n 3. SELECT THESE OPTIONS IN 'NEW RUN':")
    print(f"    Project   : {DRY_PROJECT}")
    print(f"    Architect : {DRY_ARCH}")
    print(f"    Developer : {DRY_DEV}")
    print("-" * 60)
    print(" THE RUN PAUSES FOR A HUMAN TWICE — the button depends on the node:")
    print("   * Node '07: Clarify_Review' (kind QUESTION):")
    print("       click ANSWER, type any text, then SEND ANSWER.")
    print("   * Node '11: Prepare_To_Commit' (kind COMMIT_APPROVAL):")
    print("       click APPROVE COMMIT (-> 11B: Stage_And_Commit_No_Push -> DONE).")
    print("   (Both show the 'PAUSED FOR HUMAN' badge, so go by the State/node.)")
    print("-" * 60)
    print(" WHEN DONE, TEAR DOWN (stops the Director server, kills the tmux")
    print(" sessions, and removes the sandbox folder):")
    print(f"    {Path(__file__).resolve()} --teardown")
    print("="*60 + "\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hermetic Dry-Run Sandbox Setup")
    parser.add_argument("-t", "--teardown", action="store_true", help="Kill sessions and remove sandbox directory")
    args = parser.parse_args()

    if args.teardown:
        teardown()
    else:
        setup()
