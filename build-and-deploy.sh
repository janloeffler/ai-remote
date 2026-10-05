#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RUN_TESTS=false

show_help() {
  cat <<'EOF'
Usage: ./build-and-deploy.sh [OPTIONS]

Rebuild locally (optionally running tests first and aborting on failure),
then deploy to production via deploy-production-scp.sh.

Options:
  --test       Run ./run.sh --test first; abort the deploy if tests fail
  --help, -h   Show this help
EOF
}

for arg in "$@"; do
  case "$arg" in
    --test) RUN_TESTS=true ;;
    --help|-h) show_help; exit 0 ;;
    *) echo "Unknown option: $arg"; show_help; exit 1 ;;
  esac
done

if [ "$RUN_TESTS" = true ]; then
  echo "==> Running tests (must pass before deploy)"
  ./run.sh --test
fi

echo "==> Rebuilding locally"
./run.sh --rebuild

echo "==> Deploying to production"
./deploy-production-scp.sh
