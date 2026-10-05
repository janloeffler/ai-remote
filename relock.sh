#!/bin/bash
# Regenerate the hash-pinned lockfiles from the *.in files (requires uv).
# Edit backend/requirements.in or agent/requirements.in, run this, commit the results.
#   ./relock.sh            re-resolve with a release cooldown (see below)
#   ./relock.sh --upgrade  bump everything to the newest versions allowed by the cooldown
#
# Release cooldown: releases younger than COOLDOWN_DAYS (default 7) are ignored, so a
# freshly published malicious version is unlikely to reach the lockfile before PyPI has
# removed it. Override for an urgent security fix: COOLDOWN_DAYS=0 ./relock.sh --upgrade
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
COOLDOWN_DAYS="${COOLDOWN_DAYS:-7}"
for d in backend agent; do
  for f in requirements requirements-dev; do
    uv pip compile "$d/$f.in" --universal --python-version 3.12 --generate-hashes -q \
      --exclude-newer "${COOLDOWN_DAYS} days" -o "$d/$f.txt" "$@"
  done
done
echo "Lockfiles updated (cooldown: ${COOLDOWN_DAYS} days). Run scripts/audit-deps.py before committing."
