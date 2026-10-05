#!/usr/bin/env python3
"""Stub CLI standing in for `claude`/`cursor-agent` in tests — never invokes the real
binaries. Behavior is selected via the FAKE_CLI_BEHAVIOR env var: 'ok' (default) prints
its argv to stdout and exits 0; 'fail' prints to stderr and exits 1; 'hang' sleeps
forever (used to test the agent's own subprocess timeout/kill, not the CLI's)."""
import os
import sys
import time

behavior = os.environ.get("FAKE_CLI_BEHAVIOR", "ok")

if behavior == "fail":
    print("simulated CLI failure", file=sys.stderr)
    sys.exit(1)
elif behavior == "hang":
    print("about to hang", flush=True)
    time.sleep(3600)
else:
    print(" ".join(sys.argv[1:]))
    sys.exit(0)
