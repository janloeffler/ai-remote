#!/bin/bash
# Generate API_KEY and SECRET_KEY and append them to .env.
#
#   ./generate-secrets.sh            add the keys that are missing; never touches existing values
#   ./generate-secrets.sh --rotate   replace both keys (see below)
#
# The keys are 256 bits from `openssl rand -hex 32` and are never printed, logged or
# passed on a command line (which `ps` could show). Existing .env entries are left alone:
# new values are appended at the end of the file, below a "# Generated at <date>" line.
# Later definitions win when the file is sourced, which is how the run/deploy scripts
# and docker-compose read it.
#
# --rotate: for an existing key the old line is kept but commented out
# ("# [replaced <date>] API_KEY=..."), so a tool that reads the first match cannot pick up
# the stale value. The old secret therefore stays in .env as a comment: delete those lines
# once the new keys are deployed. After rotating, re-run ./setup-agent.sh (the agent needs
# the new API_KEY) and redeploy the backend. Existing browser sessions become invalid.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
ENV_FILE="${ENV_FILE:-.env}"
umask 077

ROTATE=false
case "${1:-}" in
  "") ;;
  --rotate) ROTATE=true ;;
  -h|--help)
    echo "Usage: ./generate-secrets.sh [--rotate]"
    echo "Appends API_KEY and SECRET_KEY (256-bit random) to .env without printing them."
    echo "See the comment block at the top of this script for details."
    exit 0 ;;
  *) echo "Unknown option: $1 (try --help)" >&2; exit 1 ;;
esac

new_secret() {
  local value
  value="$(openssl rand -hex 32)"
  if [ "${#value}" -ne 64 ]; then
    echo "openssl did not return 64 hex characters; aborting." >&2
    exit 1
  fi
  printf '%s' "$value"
}

# True when the file defines KEY= with a non-empty value (quotes and blanks don't count).
has_value() {
  [ -f "$ENV_FILE" ] || return 1
  local value
  value="$(grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d " \t\"'" || true)"
  [ -n "$value" ]
}

STAMP="$(date '+%Y-%m-%d %H:%M:%S %Z')"
todo=()
for key in API_KEY SECRET_KEY; do
  if has_value "$key" && [ "$ROTATE" != true ]; then
    echo "$key: already set in $ENV_FILE, left unchanged (use --rotate to replace it)"
  else
    todo+=("$key")
  fi
done

if [ "${#todo[@]}" -eq 0 ]; then
  echo "Nothing to do."
  exit 0
fi

if [ ! -f "$ENV_FILE" ]; then
  : > "$ENV_FILE"
  echo "Created $ENV_FILE"
fi
chmod 600 "$ENV_FILE"

work="$(mktemp "${ENV_FILE}.XXXXXX")"
trap 'rm -f "$work"' EXIT
cp "$ENV_FILE" "$work"

if [ "$ROTATE" = true ]; then
  for key in "${todo[@]}"; do
    # Comment out (never delete) the old definitions.
    sed "s/^\($key=\)/# [replaced $STAMP] \1/" "$work" > "$work.next"
    mv "$work.next" "$work"
  done
fi

# Make sure the appended block starts on a fresh line.
if [ -s "$work" ] && [ -n "$(tail -c1 "$work")" ]; then
  printf '\n' >> "$work"
fi
printf '\n# Generated at %s\n' "$STAMP" >> "$work"
for key in "${todo[@]}"; do
  # A bare assignment, so a failing openssl aborts the script (set -e) instead of
  # writing an empty key. printf is a builtin: the value never appears in a process list.
  value="$(new_secret)"
  printf '%s=%s\n' "$key" "$value" >> "$work"
done
unset value

chmod 600 "$work"
mv "$work" "$ENV_FILE"
trap - EXIT

for key in "${todo[@]}"; do
  echo "$key: generated and appended to $ENV_FILE (64 hex characters, not displayed)"
done
if [ "$ROTATE" = true ]; then
  echo
  echo "Rotation checklist:"
  echo "  1. ./deploy-to-plesk.sh (or your deploy script) to update the server"
  echo "  2. ./setup-agent.sh to give the Mac agent the new API_KEY"
  echo "  3. Log in again in your browser"
  echo "  4. Delete the '# [replaced ...]' lines from $ENV_FILE"
fi
