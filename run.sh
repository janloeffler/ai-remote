#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

REBUILD=false
RUN_TESTS=false

show_help() {
  cat <<'EOF'
Usage: ./run.sh [OPTIONS]

Run the AI Remote Chat Viewer backend locally via Docker Compose, then run
one local-agent sync cycle so real Claude Code / Cursor chats show up
immediately in the browser.

Options:
  --rebuild    Force a clean rebuild of the backend image before starting
  --test       Run the backend + agent pytest suites instead of starting the app
  --help, -h   Show this help
EOF
}

for arg in "$@"; do
  case "$arg" in
    --rebuild) REBUILD=true ;;
    --test) RUN_TESTS=true ;;
    --help|-h) show_help; exit 0 ;;
    *) echo "Unknown option: $arg"; show_help; exit 1 ;;
  esac
done

ensure_venv() {
  local dir="$1"
  if [ ! -d "$dir/.venv" ]; then
    echo -e "${BLUE}==> Creating venv in $dir${NC}"
    python3 -m venv "$dir/.venv"
  fi
  # requirements-dev.txt pulls in requirements.txt plus pytest. Local venvs want the
  # test tooling; the container image installs requirements.txt only, so pytest never
  # ships to production (SEC-010).
  "$dir/.venv/bin/pip" install -q --require-hashes -r "$dir/requirements-dev.txt"
}

if [ "$RUN_TESTS" = true ]; then
  echo -e "${BLUE}==> Backend tests${NC}"
  ensure_venv backend
  (cd backend && .venv/bin/pytest -v)

  echo -e "${BLUE}==> Agent tests${NC}"
  ensure_venv agent
  (cd agent && .venv/bin/pytest -v)

  echo -e "${GREEN}All tests passed.${NC}"
  echo ""
  echo -e "${BLUE}═══ VERIFICATION ═══${NC}"
  echo -e "Backend tests: ${GREEN}passed${NC}"
  echo -e "Agent tests:   ${GREEN}passed${NC}"
  echo ""
  echo -e "${GREEN}┌─────────────────────────────┐${NC}"
  echo -e "${GREEN}│         RUNNING ✓           │${NC}"
  echo -e "${GREEN}└─────────────────────────────┘${NC}"
  echo ""
  echo "NEXT STEPS:"
  echo "  Run the app for real: ./run.sh"
  exit 0
fi

if [ ! -f .env ]; then
  echo -e "${YELLOW}==> No .env found, creating one from example.env with generated secrets${NC}"
  cp example.env .env
  sed -i '' "s/^API_KEY=.*/API_KEY=$(openssl rand -hex 32)/" .env
  sed -i '' "s/^SECRET_KEY=.*/SECRET_KEY=$(openssl rand -hex 32)/" .env
  sed -i '' "s/^SESSION_COOKIE_HTTPS_ONLY=.*/SESSION_COOKIE_HTTPS_ONLY=false/" .env
fi

set -a
# shellcheck disable=SC1091
source .env
set +a
PORT="${PORT:-8000}"

# run.sh always serves over plain http://localhost. Browsers drop cookies
# marked Secure when the connection isn't https, so force the cookie
# non-Secure here regardless of what's in .env — this overrides any stale
# .env from before this variable existed. Production (behind Plesk's HTTPS
# reverse proxy) is unaffected and keeps the secure default.
export SESSION_COOKIE_HTTPS_ONLY=false

if [ "$REBUILD" = true ]; then
  echo -e "${BLUE}==> Rebuilding backend image (no cache)${NC}"
  docker compose build --no-cache
fi

echo -e "${BLUE}==> Starting backend container${NC}"
docker compose up -d --build

echo -e "${BLUE}==> Waiting for backend to become healthy${NC}"
HEALTHY=false
for _ in $(seq 1 30); do
  if curl -s -o /dev/null -w "%{http_code}" "http://localhost:${PORT}/login" | grep -q "200"; then
    HEALTHY=true
    break
  fi
  sleep 1
done

if [ "$HEALTHY" != true ]; then
  echo -e "${RED}Backend did not become healthy within 30s. Check: docker compose logs backend${NC}"
  exit 1
fi

echo -e "${BLUE}==> Running one local-agent sync cycle against real Claude Code / Cursor data${NC}"
ensure_venv agent
(
  cd "$SCRIPT_DIR/agent"
  AI_REMOTE_BACKEND_URL="http://localhost:${PORT}" \
  AI_REMOTE_API_KEY="${API_KEY}" \
  AI_REMOTE_STATE_PATH="$SCRIPT_DIR/agent/.local_run_state.json" \
  .venv/bin/python3 -c "
from agent.main import load_config, run_cycle
import httpx
config = load_config()
with httpx.Client(timeout=15) as client:
    run_cycle(config, client)
print('sync cycle complete')
"
) || echo -e "${YELLOW}Warning: local sync cycle failed — the app is still running; retry with: (cd agent && AI_REMOTE_BACKEND_URL=http://localhost:${PORT} AI_REMOTE_API_KEY=<key> .venv/bin/python3 -m agent.main)${NC}"

echo ""
echo -e "${BLUE}═══ VERIFICATION ═══${NC}"
if [ -n "$(docker compose ps -q backend 2>/dev/null)" ]; then
  echo -e "Container running:     ${GREEN}yes${NC}"
else
  echo -e "Container running:     ${RED}no${NC}"
fi
echo -n "Health check (/login): "
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:${PORT}/login"
if nc -z localhost "${PORT}" 2>/dev/null; then
  echo -e "Port reachable:        ${GREEN}yes${NC}"
else
  echo -e "Port reachable:        ${RED}no${NC}"
fi

echo ""
echo -e "${GREEN}┌─────────────────────────────┐${NC}"
echo -e "${GREEN}│         RUNNING ✓           │${NC}"
echo -e "${GREEN}└─────────────────────────────┘${NC}"
echo ""
echo "NEXT STEPS:"
echo "  1. Open http://localhost:${PORT}/login in your browser"
echo "  2. Log in with the API key from .env (API_KEY, starts ${API_KEY:0:4}…)"
echo "  3. Your real Claude Code / Cursor sessions should already be listed"
echo ""
echo "URLS:"
echo "  Local:      http://localhost:${PORT}"
echo "  Production: ${AI_REMOTE_BACKEND_URL:-https://your-domain.example.com} (after deploy-production-scp.sh)"
echo ""
echo "COMMANDS:"
echo "  Logs:        docker compose logs -f backend"
echo "  Stop:        docker compose down"
echo "  Re-sync now: ./run.sh   (safe to re-run any time — does one sync cycle each call)"
echo "  Persistent background sync: see agent/README.md (launchd install)"
