---
name: azure-rg-diagram
description: "Inventories an Azure resource group or an ARM or Resource Graph export and generates an infrastructure diagram using Azure architecture icons in HTML and draw.io. Use when the user asks to diagram, map, visualize, or document Azure resources in a resource group or subscription, including network, compute, data, and security relationships. Not for deploying Azure resources, writing Bicep or Terraform, or non-Azure cloud diagrams."
type: workflow
lifecycle: active
---

# Azure RG Diagram — Infrastructure map from a resource group

Build a read-only infrastructure diagram from a live Azure resource group or a supplied export. Do not deploy or change resources.

## Workflow

1. Resolve scope. Use the resource group name the user gave. If they passed JSON, skip collection and go to step 3. If they named a subscription only, list resource groups and diagram each one separately unless they asked for a subscription overview.
2. Collect live inventory. See `references/collection.md`. Prefer `scripts/collect_rg.sh <rg> [subscription] <out-dir>`. If `az` is missing or not logged in, stop and ask for an export. Do not fabricate resources.
3. Build the diagram. Run:

```bash
python3 scripts/build_diagram.py --input <out-dir>/resources.json --out <out-dir>/diagram --title "<resource-group>"
```

4. Read `diagram/inventory.md` and `diagram/model.json`. Apply `references/diagram-rules.md` before showing the picture: drop governance noise, keep private endpoints, do not invent edges.
5. Deliver `diagram.html`, `diagram.drawio`, and `inventory.md`. Do not generate a Mermaid diagram. Summarize resource counts by category, name the VNets and entry points (App Gateway, Front Door, public IP, Load Balancer), and list unlinked resources as unverified — not as isolated by design.

## Outputs

| File | Show the user |
|---|---|
| diagram.html | Layered diagram with Azure architecture icons and relationship lines |
| diagram.drawio | Same icons, editable in diagrams.net |
| inventory.md | Table of resources and inferred relationships |

Open `diagram.html` for the picture. Mention that lines come from resource-id references in properties, not from guessed traffic flows. Icons load from the diagrams.net Azure architecture set.

## Scope limits

- One resource group per diagram by default. A subscription map is resource-group boxes only.
- More than 80 resources: split by VNet or by category and say the full inventory is in `inventory.md`.
- Classic (ASM) resources and Arc machines: list them; do not force them into VNet subgraphs.

## Common issues

| Problem | Action |
|---|---|
| `az` not logged in | Ask the user to run `az login`. Do not continue with fake nodes. |
| Graph query returns skip_token | Page and merge `data` before building. |
| `az resource list` fallback | Warn that properties are missing and edges will be sparse. |
| Empty resource group | Deliver an empty inventory, not a sample architecture. |
| User asks for Bicep or Terraform | This skill does not generate IaC. Diagram only. |
