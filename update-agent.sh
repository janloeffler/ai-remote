#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

LABEL="${AI_REMOTE_AGENT_LABEL:-com.example.ai-remote-agent}"
LOG_DIR="$HOME/Library/Logs"

show_help() {
  cat <<EOF
Usage: ./update-agent.sh [OPTIONS]

Bring the locally-installed agent (launchd service, via setup-agent.sh) up to
date with this checkout: pull the latest commit, refresh the venv's
dependencies, then reinstall and restart the service so the new code actually
takes effect (the running process doesn't pick up a git pull on its own).
Safe to re-run any time.

Options:
  --help, -h   Show this help
EOF
}

for arg in "$@"; do
  case "$arg" in
    --help|-h) show_help; exit 0 ;;
    *) echo "Unknown option: $arg"; show_help; exit 1 ;;
  esac
done

if [ "$(uname)" != "Darwin" ]; then
  echo -e "${RED}launchd is macOS-only; this script won't work on $(uname).${NC}"
  exit 1
fi

echo -e "${BLUE}==> Checking working tree${NC}"
if [ -n "$(git status --porcelain)" ]; then
  echo -e "${RED}Working tree has uncommitted changes — refusing to pull over them.${NC}"
  echo "Commit, stash, or discard your changes first, then re-run."
  exit 1
fi

BEFORE_SHA="$(git rev-parse --short HEAD)"

echo -e "${BLUE}==> Pulling latest (fast-forward only)${NC}"
if ! git pull --ff-only; then
  echo -e "${RED}Fast-forward failed — local branch has diverged from its upstream.${NC}"
  echo "Resolve manually (rebase/merge), then re-run."
  exit 1
fi

AFTER_SHA="$(git rev-parse --short HEAD)"

echo -e "${BLUE}==> Refreshing agent venv dependencies${NC}"
if [ ! -d agent/.venv ]; then
  python3 -m venv agent/.venv
fi
agent/.venv/bin/pip install -q --require-hashes -r agent/requirements.txt

echo -e "${BLUE}==> Reinstalling and restarting the service${NC}"
./setup-agent.sh

echo ""
echo -e "${BLUE}═══ VERIFICATION ═══${NC}"
if [ "$BEFORE_SHA" = "$AFTER_SHA" ]; then
  echo "Code:             already up to date (${AFTER_SHA}), service restarted anyway"
else
  echo "Code:             ${BEFORE_SHA} -> ${AFTER_SHA}"
fi
if launchctl list 2>/dev/null | grep -q "$LABEL"; then
  echo -e "Service loaded:   ${GREEN}yes${NC}"
else
  echo -e "Service loaded:   ${RED}no${NC}"
fi
echo "Logs:             tail -f ${LOG_DIR}/ai-remote-agent.log"
echo "                  tail -f ${LOG_DIR}/ai-remote-agent.error.log"
