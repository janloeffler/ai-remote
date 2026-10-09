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
ROTATE_PASSPHRASE=false

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
  --rotate-passphrase  E2E only (E2E_ENCRYPTION=true): set a NEW end-to-end
                       passphrase. WIPES the server cache and job history
                       (the agent resyncs) and every browser must enter the
                       new passphrase again.
  --uninstall          Stop and remove the installed service, then exit
  --help, -h           Show this help
EOF
}

for arg in "$@"; do
  case "$arg" in
    --backend-url=*) BACKEND_URL_OVERRIDE="${arg#*=}" ;;
    --api-key=*) API_KEY_OVERRIDE="${arg#*=}" ;;
    --rotate-passphrase) ROTATE_PASSPHRASE=true ;;
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

# Optional end-to-end encryption. The key is derived from a passphrase by the
# setup CLI (agent/agent/e2e_setup.py); only the derived key goes into the plist.
E2E_ENABLED=false
case "$(printf '%s' "${E2E_ENCRYPTION:-false}" | tr '[:upper:]' '[:lower:]')" in
  true|1|yes|on) E2E_ENABLED=true ;;
esac

E2E_KEY=""
E2E_KEY_ENTRY=""
if [ "$E2E_ENABLED" = true ]; then
  EXISTING_E2E_KEY=""
  if [ -f "$PLIST_DEST" ]; then
    EXISTING_E2E_KEY="$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:AI_REMOTE_E2E_KEY" "$PLIST_DEST" 2>/dev/null || true)"
  fi
  RSTFLAG=()
  STOPPED_FOR_ROTATION=false
  if [ "$ROTATE_PASSPHRASE" = true ]; then
    echo -e "${YELLOW}==> Rotating the E2E passphrase: the server cache and job history will be wiped and every browser must enter the new passphrase.${NC}"
    RSTFLAG=(--rotate)
    # Stop the running agent first so it cannot resync under the old key mid-rotation.
    if is_loaded; then
      echo -e "${YELLOW}==> Stopping the installed agent before rotating${NC}"
      launchctl bootout "$SERVICE_TARGET" 2>/dev/null || true
      STOPPED_FOR_ROTATION=true
      for _ in $(seq 1 20); do
        is_loaded || break
        sleep 0.1
      done
    fi
  fi
  echo -e "${BLUE}==> E2E encryption: checking passphrase/key${NC}"
  # stdout carries only the key (never echoed); prompts and messages use the TTY/stderr.
  if ! E2E_KEY="$(cd agent && \
      AI_REMOTE_BACKEND_URL="$BACKEND_URL" AI_REMOTE_API_KEY="$API_KEY" \
      AI_REMOTE_E2E_KEY="$EXISTING_E2E_KEY" \
      .venv/bin/python -m agent.e2e_setup ${RSTFLAG[@]+"${RSTFLAG[@]}"})"; then
    if [ "$STOPPED_FOR_ROTATION" = true ]; then
      echo -e "${RED}E2E setup failed. The agent was stopped for the rotation and is still stopped; fix the problem and re-run ./setup-agent.sh --rotate-passphrase, or re-run ./setup-agent.sh to start it with the current key.${NC}"
    else
      echo -e "${RED}E2E setup failed; no service was installed or changed.${NC}"
    fi
    echo "The server must already run with E2E_ENCRYPTION=true (deploy it first), and API_KEY/backend URL must be correct."
    exit 1
  fi
  E2E_KEY="$(printf '%s' "$E2E_KEY" | tr -d '[:space:]')"
  if [ -z "$E2E_KEY" ]; then
    if [ "$STOPPED_FOR_ROTATION" = true ]; then
      echo -e "${RED}E2E setup returned no key. The agent was stopped for the rotation and is still stopped; re-run ./setup-agent.sh --rotate-passphrase, or re-run ./setup-agent.sh to start it with the current key.${NC}"
    else
      echo -e "${RED}E2E setup returned no key; no service was installed or changed.${NC}"
    fi
    exit 1
  fi
  E2E_KEY_ENTRY="        <key>AI_REMOTE_E2E_KEY</key>
        <string>$(xml_escape "$E2E_KEY")</string>"
elif [ "$ROTATE_PASSPHRASE" = true ]; then
  echo -e "${RED}--rotate-passphrase needs E2E_ENCRYPTION=true in .env.${NC}"
  exit 1
fi
AI_REMOTE_E2E_XML="$(xml_escape "$E2E_ENABLED")"

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
        <key>AI_REMOTE_E2E</key>
        <string>${AI_REMOTE_E2E_XML}</string>
${E2E_KEY_ENTRY:+${E2E_KEY_ENTRY}
}        <key>PATH</key>
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
