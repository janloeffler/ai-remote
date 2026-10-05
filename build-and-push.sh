#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

NO_CACHE=false
SKIP_BUILD=false

show_help() {
  cat <<'EOF'
Usage: ./build-and-push.sh [OPTIONS]

Build the backend Docker image and push it to the registry configured in
.env (REGISTRY=...). Requires `docker login` to that registry beforehand.

Options:
  --no-cache    Build without Docker layer cache
  --skip-build  Skip the build step, push whatever image is already tagged locally
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
: "${REGISTRY:?Set REGISTRY in .env first, e.g. REGISTRY=ghcr.io/yourname}"
TAG="${TAG:-$(git rev-parse --short HEAD)}"
IMAGE="${REGISTRY}/ai-remote-backend:${TAG}"

if [ "$SKIP_BUILD" != true ]; then
  echo "==> Building ${IMAGE}"
  if [ "$NO_CACHE" = true ]; then
    docker build --no-cache -t "$IMAGE" backend
  else
    docker build -t "$IMAGE" backend
  fi
fi

echo "==> Pushing ${IMAGE}"
docker push "$IMAGE"
echo "Pushed ${IMAGE}"
