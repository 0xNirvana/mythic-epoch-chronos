#!/usr/bin/env bash
# Fast dev deploy: rsync source trees into Mythic InstalledServices and restart containers.
# Skips full mythic-cli rebuild — use after editing epoch/ or chronos/ locally.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MYTHIC="${MYTHIC:-$HOME/Mythic}"
INST="${MYTHIC_INSTALLED:-$MYTHIC/InstalledServices}"

bash "$ROOT/scripts/sync_install_trees.sh"

deploy_one() {
  local name="$1"
  local src="$2"
  local dst="$INST/$name"
  if [[ ! -d "$dst" ]]; then
    echo "[!] $dst missing — run mythic-cli install first (see docs/SETUP.md Step 5)"
    cd "$MYTHIC"
    sudo ./mythic-cli install folder "$ROOT/install/$name"
    return
  fi
  echo "[*] rsync $name -> $dst"
  sudo rsync -a --delete \
    --exclude 'c2_code/credentials.json' \
    --exclude 'c2_code/config.json' \
    --exclude 'c2_code/server.log' \
    --exclude 'c2_code/state.json' \
    "$src/" "$dst/"
}

deploy_one chronos "$ROOT/chronos"
deploy_one epoch "$ROOT/epoch"

echo "[*] restarting containers"
docker restart chronos epoch 2>/dev/null || true
echo "[+] dev deploy done. Rebuild Chronos payload if Epoch C2 config changed."
