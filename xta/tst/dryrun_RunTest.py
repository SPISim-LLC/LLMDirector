#!/usr/bin/env python3
"""Dry-run stub for the driven project's RunTest.py.
Always reports PASS so Validate_Implementation advances cleanly.
Copied into the dry-run project folder by dryrun_setup.sh."""
import sys, argparse, time

parser = argparse.ArgumentParser()
parser.add_argument('-a', '--all', action='store_true')
args = parser.parse_args()
if not args.all:
    print("Usage: ./xta/tst/RunTest.py -a"); sys.exit(1)

time.sleep(1)  # simulate a short test run
print("DryRun: validation stub — always PASS.")
print("\nSummary")
print("  Report  : xta/tst/TestResult.html")
print("  SUCCESS : 1")
print("  SKIPPED : 0")
print("  FAIL    : 0")
print("  Total   : 1")
print("  Elapsed : 1s")
sys.exit(0)
