# Collection

Read this when the user has not already supplied a resource export.

## Preconditions

- Azure CLI logged in (`az account show`). If not, stop and ask the user to run `az login`. Do not invent credentials.
- Reader on the resource group. Resource Graph Reader is enough for `az graph query`.
- Do not deploy, modify, or delete resources. This skill is read-only.

## Preferred query

Resource Graph returns `properties`, which the diagram builder uses for edges.

```bash
az graph query -q "Resources | where resourceGroup =~ 'RG_NAME' | project id, name, type, location, kind, sku, tags, properties, resourceGroup, subscriptionId, managedBy" --first 1000 -o json > resources.json
```

Or run `scripts/collect_rg.sh RG_NAME [subscription] ./azure-rg-export`.

## Pagination

If the result has a `skip_token`, query again with `--skip-token` and merge `data` arrays before building the diagram. Do not silently drop pages.

## Subscription scope

Only when the user asks for every resource group in a subscription:

```bash
az graph query -q "Resources | where subscriptionId =~ 'SUB_ID' | project id, name, type, location, resourceGroup, properties" --first 1000 -o json
```

Emit one diagram per resource group unless the user asked for a subscription map. A subscription map uses resource-group containers, not every NIC.

## Fallback

`az resource list -g RG_NAME -o json` has no properties. Tell the user edges will be incomplete and still produce the inventory grouped by type.

## Portal export

ARM exports (`{"resources": [...]}`) are accepted. Nested VNet subnets are expanded by the builder. Template parameters are not live inventory — label the diagram "ARM export", not "live".
