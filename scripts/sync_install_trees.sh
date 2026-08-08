#!/usr/bin/env bash
# Populate mythic-cli install trees from source (Payload_Type/ + C2_Profiles/).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHR="$ROOT/install/chronos/Payload_Type/chronos"
EPO="$ROOT/install/epoch/C2_Profiles/epoch"
rm -rf "$CHR" "$EPO"
mkdir -p "$(dirname "$CHR")" "$(dirname "$EPO")"
rsync -a --delete \
  --exclude '.git' \
  "$ROOT/chronos/" "$CHR/"
rsync -a --delete \
  --exclude '.git' \
  "$ROOT/epoch/" "$EPO/"
# Canonical protocol module lives in shared/; propagate to source + install trees.
cp "$ROOT/shared/protocol_v2.py" "$ROOT/chronos/chronos/agent_code/protocol_v2.py"
cp "$ROOT/shared/protocol_v2.py" "$ROOT/epoch/c2_code/protocol_v2.py"
cp "$ROOT/shared/protocol_v2.py" "$CHR/chronos/agent_code/protocol_v2.py"
cp "$ROOT/shared/protocol_v2.py" "$EPO/c2_code/protocol_v2.py"
echo "[+] install trees ready:"
echo "    $ROOT/install/chronos   (mythic-cli install folder …/install/chronos)"
echo "    $ROOT/install/epoch      (mythic-cli install folder …/install/epoch)"
