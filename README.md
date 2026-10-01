# azure-skill

Agent skills for Azure work.

## azure-rg-diagram

Read-only skill that inventories an Azure resource group (or an ARM / Resource Graph export) and generates an infrastructure diagram.

Outputs:

- `diagram.mmd` — Mermaid source
- `diagram.html` — browser preview
- `diagram.drawio` — editable diagrams.net file
- `inventory.md` — resources and inferred relationships

Edges are drawn only when one resource's properties contain another resource's ID. The skill does not deploy or modify Azure resources.

```bash
azure-rg-diagram/scripts/collect_rg.sh <resource-group> [subscription] ./azure-rg-export
python3 azure-rg-diagram/scripts/build_diagram.py \
  --input ./azure-rg-export/resources.json \
  --out ./azure-rg-export/diagram \
  --title "<resource-group>"
```

Sample input: `azure-rg-diagram/assets/sample-rg.json`.
