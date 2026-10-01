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

## Sample run

Input is the checked-in export `azure-rg-diagram/assets/sample-rg.json` (a Resource Graph-shaped payload for `rg-prod`: VNet, two subnets, NSG, load balancer, public IP, NIC, VM, private endpoint, SQL server, Key Vault).

```bash
python3 azure-rg-diagram/scripts/build_diagram.py \
  --input azure-rg-diagram/assets/sample-rg.json \
  --out ./sample-out \
  --title rg-prod
```

Result: **11 resources, 9 edges**. Nested subnets are expanded even though they were not separate rows. Key Vault stays unlinked because the export has no resource-id reference to it.

```mermaid
flowchart TB
  subgraph network[network]
    n0["vnet-app"]
    n9["snet-web"]
    n10["snet-data"]
    n1["nsg-web"]
    n3["lb-web"]
    n2["pip-lb"]
    n4["nic-web-01"]
    n7["pe-sql"]
  end
  subgraph compute[compute]
    n5["vm-web-01"]
  end
  subgraph data[data]
    n6["sql-app"]
  end
  subgraph security[security]
    n8["kv-app"]
  end
  n0 -->|subnets| n9
  n0 -->|subnets| n10
  n0 -->|network security group| n1
  n9 -->|network security group| n1
  n3 -->|public ipaddress| n2
  n4 -->|subnet| n9
  n5 -->|network interfaces| n4
  n7 -->|subnet| n10
  n7 -->|private link service id| n6
```

Relationships written to `inventory.md`:

- vnet-app → snet-web, snet-data, nsg-web
- snet-web → nsg-web
- lb-web → pip-lb
- nic-web-01 → snet-web
- vm-web-01 → nic-web-01
- pe-sql → snet-data, sql-app
- unlinked: kv-app
