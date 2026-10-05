#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/docker-images"
IMAGE_NAME="${IMAGE_NAME:-ai-remote-backend:latest}"
PLATFORM="${PLATFORM:-linux/amd64}"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
TAR_PATH="$OUTPUT_DIR/ai-remote-backend-${TIMESTAMP}.tar"
ARCHIVE_PATH="${TAR_PATH}.gz"

usage() {
  cat <<'EOF'
AI Remote Chat Viewer – Docker-Image bauen und exportieren

VERWENDUNG
  ./build-container.sh [OPTION]

OPTIONEN
  (keine)       Baut das Image fuer linux/amd64 (Standardziel: Plesk-Server)
  --local       Baut das Image fuer die lokale Architektur (z.B. ARM auf Apple Silicon)
  -h, --help    Zeigt diese Hilfe an

UMGEBUNGSVARIABLEN
  IMAGE_NAME    Docker-Image-Name   (Standard: ai-remote-backend:latest)
  PLATFORM      Zielplattform       (Standard: linux/amd64)

BEISPIELE
  ./build-container.sh
      Baut fuer linux/amd64 und exportiert nach docker-images/

  ./build-container.sh --local
      Baut fuer die native Architektur des aktuellen Rechners

  PLATFORM=linux/arm64 ./build-container.sh
      Baut explizit fuer ARM64
EOF
}

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
  --local)
    PLATFORM=""
    echo "Baue Docker-Image fuer lokale Architektur: $IMAGE_NAME"
    ;;
  "")
    echo "Baue Docker-Image fuer $PLATFORM: $IMAGE_NAME"
    ;;
  *)
    echo "Unbekanntes Argument: $1" >&2
    usage
    exit 1
    ;;
esac

mkdir -p "$OUTPUT_DIR"

BUILD_TIMESTAMP="$(date '+%Y-%m-%d %H:%M')"

if [ -n "$PLATFORM" ]; then
  docker build --platform "$PLATFORM" --build-arg BUILD_TIMESTAMP="$BUILD_TIMESTAMP" -t "$IMAGE_NAME" "$SCRIPT_DIR/backend"
else
  docker build --build-arg BUILD_TIMESTAMP="$BUILD_TIMESTAMP" -t "$IMAGE_NAME" "$SCRIPT_DIR/backend"
fi

echo "Exportiere Image nach: $ARCHIVE_PATH"
docker save "$IMAGE_NAME" -o "$TAR_PATH"
gzip -f "$TAR_PATH"

echo
echo "Fertig: $ARCHIVE_PATH"
echo "Import in Plesk:"
echo "  Plesk > Docker > Image importieren > $(basename "$ARCHIVE_PATH") hochladen"
echo
echo "Container-Einstellungen in Plesk (Beispielwerte, an deinen Server anpassen):"
echo "  Manual mapping:"
echo "    Container-Port 8000 -> External Port 32876"
echo "    \"Make the port inaccessible from the internet\": JA ankreuzen"
echo "      (TLS wird von Plesks eigenem Reverse-Proxy fuer die Domain terminiert;"
echo "       der Container-Port darf nicht direkt aus dem Internet erreichbar sein)"
echo "  Volume mapping:"
echo "    Host:      /opt/ai-remote-backend/data   (vorher per SSH anlegen: mkdir -p /opt/ai-remote-backend/data)"
echo "    Container: /data"
echo "      (persistiert die SQLite-Datenbank — ohne dieses Volume gehen Daten bei jedem Container-Neustart verloren)"
echo "  Umgebungsvariablen:"
echo "    API_KEY=<dein-api-key>            (gleicher Wert wie in .env / setup-agent.sh)"
echo "    SECRET_KEY=<dein-secret-key>"
echo "    DATABASE_PATH=/data/app.db"
echo "    LOCAL_HOME_DIR=/Users/yourname"
echo "    CHAT_HISTORY_PAGE_SIZE=10"
echo
echo "Danach einmalig (falls noch nicht geschehen): deine Domain (z.B. your-domain.example.com) in Plesk"
echo "auf den External Port 32876 dieses Containers binden und Let's Encrypt SSL aktivieren."
