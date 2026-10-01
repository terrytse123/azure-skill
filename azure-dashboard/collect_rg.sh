#!/usr/bin/env bash
# Collect a resource-group export for azure-dashboard.html. Read-only.
set -euo pipefail
RG="${1:?resource group required}"
SUB="${2:-}"
OUT="${3:-./azure-export}"
mkdir -p "$OUT"
ARGS=(resource list -g "$RG" --query "[].{id:id,name:name,type:type,location:location,resourceGroup:resourceGroup}" -o json)
if [[ -n "$SUB" ]]; then ARGS+=(--subscription "$SUB"); fi
az "${ARGS[@]}" > "$OUT/resources.json"
python3 - << PY
import json
from pathlib import Path
rows = json.loads(Path("$OUT/resources.json").read_text())
Path("$OUT/dashboard.json").write_text(json.dumps({"data": rows}, indent=2))
print(f"wrote $OUT/dashboard.json ({len(rows)} resources)")
print("Open azure-dashboard.html and use Load export.")
PY
