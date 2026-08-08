#!/usr/bin/env bash
# Export a clean public-ready copy with a single commit attributed to the maintainer.
#
# GitHub: one repo name per account. If mythic-epoch-chronos already exists (private),
# rename it on GitHub first (e.g. mythic-epoch-chronos-dev) before creating the public repo.
#
# Usage:
#   bash scripts/prepare_public_repo.sh [destination_dir]
#
# Then (from destination, with gh authenticated as 0xNirvana):
#   gh repo create mythic-epoch-chronos --public --source=. --remote=origin --push
#
# Pusher vs author: commit author is set below. GitHub "contributions" also require
# pushes via SSH/HTTPS logged in as 0xNirvana (not the ubuntu EC2 default identity).

set -euo pipefail

AUTHOR_NAME="${GIT_AUTHOR_NAME:-0xNirvana}"
AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-57602228+0xNirvana@users.noreply.github.com}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$(dirname "$ROOT")/mythic-epoch-chronos-public}"

echo "[*] Source:  $ROOT"
echo "[*] Dest:    $DEST"
echo "[*] Author:  $AUTHOR_NAME <$AUTHOR_EMAIL>"

rm -rf "$DEST"
mkdir -p "$DEST"

rsync -a \
  --exclude '.git' \
  --exclude 'checkin_suite_a' \
  --exclude 'install/chronos/Payload_Type' \
  --exclude 'install/epoch/C2_Profiles' \
  --exclude '.venv' \
  --exclude 'venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.DS_Store' \
  --exclude 'CURSOR_HANDOFF.md' \
  --exclude '*_HANDOFF.md' \
  "$ROOT/" "$DEST/"

cd "$DEST"
git init -b main

# Repo-local identity for all future commits in the public clone
git config user.name "$AUTHOR_NAME"
git config user.email "$AUTHOR_EMAIL"

export GIT_AUTHOR_NAME="$AUTHOR_NAME"
export GIT_AUTHOR_EMAIL="$AUTHOR_EMAIL"
export GIT_COMMITTER_NAME="$AUTHOR_NAME"
export GIT_COMMITTER_EMAIL="$AUTHOR_EMAIL"

git add -A
git status

git commit -m "$(cat <<'EOF'
Initial public release: Mythic Epoch + Chronos

Google Calendar dead-drop C2 for Mythic — Epoch relay profile and Chronos agent.
EOF
)"

echo ""
echo "[+] Clean public repo ready at: $DEST"
echo "[+] Single commit on main — author: $AUTHOR_NAME <$AUTHOR_EMAIL>"
echo ""
echo "Next steps:"
echo "  1. cd $DEST"
echo "  2. gh auth login   # as 0xNirvana (or create repo via GitHub UI)"
echo "  3. gh repo create mythic-epoch-chronos --public --source=. --remote=origin --push"
echo "  4. git tag v0.2.1-workshop && git push origin v0.2.1-workshop"
echo ""
echo "Optional — fix author on the private dev repo for future commits:"
echo "  cd $ROOT && git config user.name \"$AUTHOR_NAME\" && git config user.email \"$AUTHOR_EMAIL\""
