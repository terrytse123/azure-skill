#!/usr/bin/env bash
# Collect an Azure resource group into resources.json for build_diagram.py.
# Usage: collect_rg.sh <resource-group> [subscription-id-or-name] [output-dir]
set -euo pipefail

RG="${1:-}"
SUB="${2:-}"
OUT="${3:-./azure-rg-export}"

if [[ -z "$RG" ]]; then
  echo "Usage: collect_rg.sh <resource-group> [subscription] [output-dir]" >&2
  exit 2
fi

if ! command -v az >/dev/null 2>&1; then
  echo "Azure CLI is not installed. Export JSON from the portal or Resource Graph and pass it to build_diagram.py." >&2
  exit 3
fi

if ! az account show >/dev/null 2>&1; then
  echo "Not logged in. Run: az login" >&2
  exit 4
fi

if [[ -n "$SUB" ]]; then
  az account set --subscription "$SUB"
fi

mkdir -p "$OUT"
QUERY="Resources | where resourceGroup =~ '${RG}' | project id, name, type, location, kind, sku, tags, properties, resourceGroup, subscriptionId, managedBy"

if az graph query -q "$QUERY" --first 1000 -o json >"$OUT/resources.json" 2>"$OUT/graph.err"; then
  echo "Collected Resource Graph results to $OUT/resources.json"
else
  echo "Resource Graph query failed; falling back to az resource list." >&2
  cat "$OUT/graph.err" >&2 || true
  az resource list -g "$RG" --query "[].{id:id,name:name,type:type,location:location,kind:kind,sku:sku,tags:tags,resourceGroup:resourceGroup}" -o json >"$OUT/resources.json"
  echo "Warning: az resource list omits properties, so relationship edges will be sparse." >&2
fi

echo "$RG" >"$OUT/resource-group.txt"
echo "Next: python3 build_diagram.py --input $OUT/resources.json --out $OUT/diagram --title $RG"
