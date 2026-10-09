#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Example target container configuration below — adjust to match your own
# Plesk Docker container (verify with `docker inspect <container-name>` on
# your server):
#   Name:   ai-remote-backend
#   Image:  ai-remote-backend:latest
#   Port:   127.0.0.1:32876 -> 8000   (Plesk-Reverse-Proxy terminiert TLS,
#           Container-Port bleibt vom Internet aus unerreichbar)
#   Volume: /opt/ai-remote-backend/data -> /data
#   Restart: unless-stopped
CONTAINER_NAME="ai-remote-backend"
IMAGE_NAME="ai-remote-backend:latest"
REMOTE_BASE="/opt/ai-remote-backend"
HOST_PORT=32876
CONTAINER_PORT=8000

SKIP_BUILD=false

show_help() {
  cat <<'EOF'
Usage: ./deploy-to-plesk.sh [OPTIONS]

Baut das Backend-Image via ./build-container.sh und aktualisiert den
laufenden Container auf deinem Server direkt per SSH:
docker load des exportierten Images, danach Container stoppen/entfernen
und mit demselben Port-/Volume-/Env-Setup neu starten (kein Registry,
kein manueller Upload ueber die Plesk-UI noetig).

Options:
  --skip-build  Kein neuer Build; das neueste Archiv aus docker-images/
                wird verwendet
  --help, -h    Diese Hilfe anzeigen
EOF
}

for arg in "$@"; do
  case "$arg" in
    --skip-build) SKIP_BUILD=true ;;
    --help|-h) show_help; exit 0 ;;
    *) echo "Unbekannte Option: $arg"; show_help; exit 1 ;;
  esac
done

[ -f .env ] && { set -a; source .env; set +a; }
: "${PLESK_HOST:?Set PLESK_HOST in .env first, e.g. PLESK_HOST=root@your-server.example.com}"
: "${API_KEY:?API_KEY in .env setzen}"
: "${SECRET_KEY:?SECRET_KEY in .env setzen}"

if [ "$SKIP_BUILD" != true ]; then
  echo "==> Baue Image via build-container.sh"
  ./build-container.sh
fi

ARCHIVE="$(ls -t docker-images/*.tar.gz 2>/dev/null | head -1 || true)"
[ -n "$ARCHIVE" ] || { echo "Kein Archiv in docker-images/ gefunden. Erst ohne --skip-build laufen lassen." >&2; exit 1; }
ARCHIVE_BASENAME="$(basename "$ARCHIVE")"

# SESSION_COOKIE_HTTPS_ONLY is forced to true on purpose: run.sh rewrites it to false in the
# local .env for http://localhost testing, and shipping that value would send the session
# cookie without the Secure flag from the internet-facing instance.
echo "==> Erzeuge Runtime-Env-Datei"
ENV_FILE="$(mktemp)"
cat > "$ENV_FILE" <<EOF
API_KEY=${API_KEY}
SECRET_KEY=${SECRET_KEY}
LOCAL_HOME_DIR=${LOCAL_HOME_DIR:-/Users/yourname}
CHAT_HISTORY_PAGE_SIZE=${CHAT_HISTORY_PAGE_SIZE:-10}
AI_REMOTE_INTERVAL_SECONDS=${AI_REMOTE_INTERVAL_SECONDS:-60}
AI_REMOTE_ACTIVE_INTERVAL_SECONDS=${AI_REMOTE_ACTIVE_INTERVAL_SECONDS:-10}
ACTIVE_INTERVAL_DURATION_MIN=${ACTIVE_INTERVAL_DURATION_MIN:-5}
AI_REMOTE_ALLOWED_PROJECTS=${AI_REMOTE_ALLOWED_PROJECTS:-}
SESSION_COOKIE_HTTPS_ONLY=true
TRUSTED_PROXY_HOPS=${TRUSTED_PROXY_HOPS:-0}
TRUSTED_PROXIES=${TRUSTED_PROXIES:-}
CLAUDE_CODE_ENABLED=${CLAUDE_CODE_ENABLED:-true}
CURSOR_ENABLED=${CURSOR_ENABLED:-true}
DEFAULT_AI=${DEFAULT_AI:-claude}
IMAGE_UPLOAD_ENABLED=${IMAGE_UPLOAD_ENABLED:-false}
E2E_ENCRYPTION=${E2E_ENCRYPTION:-false}
IMAGE_RETENTION_DAYS=${IMAGE_RETENTION_DAYS:-3}
EOF
trap 'rm -f "$ENV_FILE"' EXIT

echo "==> Kopiere Image nach ${PLESK_HOST}:/tmp/${ARCHIVE_BASENAME}"
ssh "$PLESK_HOST" "mkdir -p ${REMOTE_BASE}/data"
scp "$ARCHIVE" "${PLESK_HOST}:/tmp/${ARCHIVE_BASENAME}"

echo "==> Kopiere Env-Datei nach ${PLESK_HOST}:${REMOTE_BASE}/.env"
# `install -m600` rather than scp: the file holds API_KEY/SECRET_KEY and scp would
# create it with the remote default umask on a shared host (SEC-008).
ssh "$PLESK_HOST" "install -m600 /dev/stdin ${REMOTE_BASE}/.env" < "$ENV_FILE"

echo "==> Lade Image und starte Container neu auf ${PLESK_HOST}"
# shellcheck disable=SC2087
ssh "$PLESK_HOST" bash -s <<EOF
set -euo pipefail
docker load -i "/tmp/${ARCHIVE_BASENAME}"
rm -f "/tmp/${ARCHIVE_BASENAME}"

docker stop "${CONTAINER_NAME}" >/dev/null 2>&1 || true
docker rm "${CONTAINER_NAME}" >/dev/null 2>&1 || true

docker run -d \
  --name "${CONTAINER_NAME}" \
  --restart unless-stopped \
  -p 127.0.0.1:${HOST_PORT}:${CONTAINER_PORT} \
  -v "${REMOTE_BASE}/data:/data" \
  --env-file "${REMOTE_BASE}/.env" \
  "${IMAGE_NAME}"

docker image prune -f >/dev/null
docker ps --filter "name=${CONTAINER_NAME}" --format 'Laeuft: {{.Names}}  {{.Image}}  {{.Ports}}  {{.Status}}'
EOF

echo
echo "Fertig. Health-Check:"
echo "  curl -s -o /dev/null -w '%{http_code}\n' ${AI_REMOTE_BACKEND_URL:-https://your-domain.example.com}/login"
echo
echo "Hinweis: Der Container bekommt dabei eine neue Container-ID (docker stop/rm/run,"
echo "nicht Plesks eigener 'Update'-Mechanismus). Die Domain-Bindung in Plesk zeigt auf"
echo "den Host-Port ${HOST_PORT}, nicht auf die Container-ID, daher bleibt ${AI_REMOTE_BACKEND_URL:-https://your-domain.example.com}"
echo "erreichbar. Falls die Plesk-UI (Docker > Container) den Container danach als"
echo "'nicht verwaltet' anzeigt, einmal die Seite neu laden bzw. den Container in der"
echo "Liste antippen - die Zuordnung basiert auf dem Namen/Port, nicht auf einer"
echo "gespeicherten Container-ID."
