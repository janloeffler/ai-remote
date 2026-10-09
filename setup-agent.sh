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
PLIST_DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN_TARGET="gui/$(id -u)"
SERVICE_TARGET="${DOMAIN_TARGET}/${LABEL}"
LOG_DIR="$HOME/Library/Logs"
DEFAULT_BACKEND_URL="https://your-domain.example.com"
BACKEND_URL_OVERRIDE=""
API_KEY_OVERRIDE=""
UNINSTALL=false

show_help() {
  cat <<EOF
Usage: ./setup-agent.sh [OPTIONS]

Install the local agent (agent/agent/main.py) as a persistent background
service via launchd, so it keeps polling the backend for sync/job work
(including "Mehr laden" / "Gesamte Historie laden" jobs) without a terminal
open. Safe to re-run any time — it reinstalls cleanly over an existing
install.

Options:
  --backend-url URL   Point the agent at a different backend (default:
                       AI_REMOTE_BACKEND_URL from .env, falling back to
                       ${DEFAULT_BACKEND_URL})
  --api-key KEY        Use this API key instead of reading API_KEY from .env
  --uninstall          Stop and remove the installed service, then exit
  --help, -h           Show this help
EOF
}

for arg in "$@"; do
  case "$arg" in
    --backend-url=*) BACKEND_URL_OVERRIDE="${arg#*=}" ;;
    --api-key=*) API_KEY_OVERRIDE="${arg#*=}" ;;
    --uninstall) UNINSTALL=true ;;
    --help|-h) show_help; exit 0 ;;
    *) echo "Unknown option: $arg"; show_help; exit 1 ;;
  esac
done

is_loaded() {
  launchctl list 2>/dev/null | grep -q "$LABEL"
}

if [ "$UNINSTALL" = true ]; then
  echo -e "${BLUE}==> Uninstalling ${LABEL}${NC}"
  if is_loaded; then
    launchctl bootout "$SERVICE_TARGET"
  fi
  rm -f "$PLIST_DEST"
  echo -e "${GREEN}Removed. Agent will no longer run in the background.${NC}"
  exit 0
fi

if [ "$(uname)" != "Darwin" ]; then
  echo -e "${RED}launchd is macOS-only; this script won't work on $(uname).${NC}"
  echo "See agent/README.md for a manual/cron-based alternative."
  exit 1
fi

echo -e "${BLUE}==> Setting up agent venv${NC}"
if [ ! -d agent/.venv ]; then
  python3 -m venv agent/.venv
fi
agent/.venv/bin/pip install -q --require-hashes -r agent/requirements.txt

API_KEY="${API_KEY_OVERRIDE:-${API_KEY:-}}"
if [ -z "$API_KEY" ]; then
  echo -e "${RED}API_KEY is empty.${NC}"
  echo "Set it in .env (cp example.env .env && edit it — see run.sh), or pass --api-key=<key>."
  exit 1
fi

BACKEND_URL="${BACKEND_URL_OVERRIDE:-${AI_REMOTE_BACKEND_URL:-$DEFAULT_BACKEND_URL}}"

# Escape XML special characters — cheap insurance in case the key ever
# contains them, even though generated keys (openssl rand -hex) never do.
xml_escape() {
  local s="$1"
  s="${s//&/&amp;}"
  s="${s//</&lt;}"
  s="${s//>/&gt;}"
  printf '%s' "$s"
}
API_KEY_XML="$(xml_escape "$API_KEY")"
BACKEND_URL_XML="$(xml_escape "$BACKEND_URL")"
ALLOWED_PROJECTS_XML="$(xml_escape "${AI_REMOTE_ALLOWED_PROJECTS:-}")"
CLAUDE_CODE_ENABLED_XML="$(xml_escape "${CLAUDE_CODE_ENABLED:-true}")"
CURSOR_ENABLED_XML="$(xml_escape "${CURSOR_ENABLED:-true}")"
DEFAULT_AI_XML="$(xml_escape "${DEFAULT_AI:-claude}")"
IMAGE_UPLOAD_ENABLED_XML="$(xml_escape "${IMAGE_UPLOAD_ENABLED:-false}")"

echo -e "${BLUE}==> Writing ${PLIST_DEST}${NC}"
mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

if is_loaded; then
  echo -e "${YELLOW}==> Already installed — unloading old version first${NC}"
  launchctl bootout "$SERVICE_TARGET" 2>/dev/null || true
  # bootout returns before launchd has fully torn the old instance down;
  # bootstrapping the new one too soon after is what causes the (usually
  # harmless, but confusing) "Input/output error" from launchctl load/unload.
  for _ in $(seq 1 20); do
    is_loaded || break
    sleep 0.1
  done
fi

cat > "$PLIST_DEST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${SCRIPT_DIR}/agent/.venv/bin/python3</string>
        <string>-m</string>
        <string>agent.main</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}/agent</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>AI_REMOTE_BACKEND_URL</key>
        <string>${BACKEND_URL_XML}</string>
        <key>AI_REMOTE_API_KEY</key>
        <string>${API_KEY_XML}</string>
        <key>AI_REMOTE_ALLOWED_PROJECTS</key>
        <string>${ALLOWED_PROJECTS_XML}</string>
        <key>CLAUDE_CODE_ENABLED</key>
        <string>${CLAUDE_CODE_ENABLED_XML}</string>
        <key>CURSOR_ENABLED</key>
        <string>${CURSOR_ENABLED_XML}</string>
        <key>DEFAULT_AI</key>
        <string>${DEFAULT_AI_XML}</string>
        <key>IMAGE_UPLOAD_ENABLED</key>
        <string>${IMAGE_UPLOAD_ENABLED_XML}</string>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:${HOME}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/ai-remote-agent.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/ai-remote-agent.error.log</string>
</dict>
</plist>
PLIST
chmod 600 "$PLIST_DEST"

echo -e "${BLUE}==> Loading service${NC}"
launchctl bootstrap "$DOMAIN_TARGET" "$PLIST_DEST"

sleep 1

echo ""
echo -e "${BLUE}═══ VERIFICATION ═══${NC}"
if is_loaded; then
  echo -e "Service loaded:   ${GREEN}yes${NC}"
else
  echo -e "Service loaded:   ${RED}no${NC}"
fi
echo "Backend URL:      ${BACKEND_URL}"
echo "Logs:             ${LOG_DIR}/ai-remote-agent.log"
echo "                  ${LOG_DIR}/ai-remote-agent.error.log"

echo ""
echo -e "${GREEN}┌─────────────────────────────┐${NC}"
echo -e "${GREEN}│         RUNNING ✓           │${NC}"
echo -e "${GREEN}└─────────────────────────────┘${NC}"
echo ""
echo "NEXT STEPS:"
echo "  Watch it work:  tail -f ${LOG_DIR}/ai-remote-agent.log"
echo "  Check status:   launchctl list | grep ${LABEL}"
echo "  Reinstall:      ./setup-agent.sh   (safe to re-run any time)"
echo "  Remove:         ./setup-agent.sh --uninstall"
