#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

NO_CACHE=false
SKIP_BUILD=false

show_help() {
  cat <<'EOF'
Usage: ./deploy-production-scp.sh [OPTIONS]

Build the backend image, ship it to the production server via SCP (docker
save | ssh | docker load — no registry needed), write a minimal production
compose file there, and restart the container via SSH.

Requires SSH_HOST (and optionally SSH_PATH) set in .env, and passwordless
SSH access to that host.

Options:
  --no-cache    Build without Docker layer cache
  --skip-build  Skip building/shipping the image; only rewrite the compose
                file and restart whatever image is already loaded on the server
  --help, -h    Show this help
EOF
}

for arg in "$@"; do
  case "$arg" in
    --no-cache) NO_CACHE=true ;;
    --skip-build) SKIP_BUILD=true ;;
    --help|-h) show_help; exit 0 ;;
    *) echo "Unknown option: $arg"; show_help; exit 1 ;;
  esac
done

[ -f .env ] && { set -a; source .env; set +a; }
: "${SSH_HOST:?Set SSH_HOST in .env first, e.g. SSH_HOST=your-domain.example.com}"
SSH_PATH="${SSH_PATH:-/opt/ai-remote-backend}"
IMAGE_TAG="ai-remote-backend:$(git rev-parse --short HEAD)"

if [ "$SKIP_BUILD" != true ]; then
  echo "==> Building ${IMAGE_TAG}"
  if [ "$NO_CACHE" = true ]; then
    docker build --no-cache -t "$IMAGE_TAG" backend
  else
    docker build -t "$IMAGE_TAG" backend
  fi

  echo "==> Shipping image to ${SSH_HOST} via SCP"
  docker save "$IMAGE_TAG" | gzip | ssh "$SSH_HOST" "gunzip | docker load"
fi

echo "==> Writing production compose file on ${SSH_HOST}"
ssh "$SSH_HOST" "mkdir -p ${SSH_PATH}/data"
cat > /tmp/ai-remote-compose.prod.yml <<COMPOSE
services:
  backend:
    image: ${IMAGE_TAG}
    ports:
      # Bind to localhost only: Plesk's reverse proxy (which terminates TLS)
      # is the only thing that should reach this container. Binding the
      # public interface directly would let anyone bypass TLS entirely.
      - "127.0.0.1:\${PORT:-8000}:8000"
    volumes:
      - ./data:/data
    environment:
      - API_KEY=\${API_KEY}
      - SECRET_KEY=\${SECRET_KEY}
      - DATABASE_PATH=/data/app.db
      - TRUSTED_PROXY_HOPS=\${TRUSTED_PROXY_HOPS:-0}
      - TRUSTED_PROXIES=\${TRUSTED_PROXIES:-}
      - LOCAL_HOME_DIR=\${LOCAL_HOME_DIR:-/Users/yourname}
      - CHAT_HISTORY_PAGE_SIZE=\${CHAT_HISTORY_PAGE_SIZE:-10}
      - AI_REMOTE_ALLOWED_PROJECTS=\${AI_REMOTE_ALLOWED_PROJECTS:-}
      - AI_REMOTE_INTERVAL_SECONDS=\${AI_REMOTE_INTERVAL_SECONDS:-60}
      - AI_REMOTE_ACTIVE_INTERVAL_SECONDS=\${AI_REMOTE_ACTIVE_INTERVAL_SECONDS:-10}
      - ACTIVE_INTERVAL_DURATION_MIN=\${ACTIVE_INTERVAL_DURATION_MIN:-5}
      - CLAUDE_CODE_ENABLED=\${CLAUDE_CODE_ENABLED:-true}
      - CURSOR_ENABLED=\${CURSOR_ENABLED:-true}
      - DEFAULT_AI=\${DEFAULT_AI:-claude}
      - IMAGE_UPLOAD_ENABLED=\${IMAGE_UPLOAD_ENABLED:-false}
      - IMAGE_RETENTION_DAYS=\${IMAGE_RETENTION_DAYS:-3}
    restart: unless-stopped
COMPOSE
scp /tmp/ai-remote-compose.prod.yml "${SSH_HOST}:${SSH_PATH}/docker-compose.yml"
# .env carries API_KEY and SECRET_KEY. scp creates the remote file with the default
# umask, which on a shared host (Plesk runs several site users) can leave the key to
# remote command execution world-readable — so write it through `install -m600`
# instead of leaving a permissive file behind (SEC-008).
# Force Secure cookies: run.sh sets SESSION_COOKIE_HTTPS_ONLY=false in the local .env for
# http://localhost testing, which must never reach the internet-facing instance.
{ grep -v '^SESSION_COOKIE_HTTPS_ONLY=' .env; echo "SESSION_COOKIE_HTTPS_ONLY=true"; } \
  | ssh "$SSH_HOST" "install -m600 /dev/stdin ${SSH_PATH}/.env"
rm -f /tmp/ai-remote-compose.prod.yml

echo "==> Restarting container on ${SSH_HOST}"
ssh "$SSH_HOST" "cd ${SSH_PATH} && docker compose up -d"

echo "Deployed ${IMAGE_TAG} to ${SSH_HOST}:${SSH_PATH}"
echo "Verify: curl -s -o /dev/null -w '%{http_code}\n' ${AI_REMOTE_BACKEND_URL:-https://your-domain.example.com}/login"
