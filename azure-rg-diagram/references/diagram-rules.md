# Diagram rules

Read this when deciding layout, edge labels, or what to omit.

## Layers

| Category | Type prefixes | Draw left-to-right |
|---|---|---|
| network | Microsoft.Network | 1 |
| compute | Microsoft.Compute, ContainerService, Web, App | 2 |
| data | Sql, DocumentDB, DBfor*, Cache, Storage, Synapse | 3 |
| integration | ServiceBus, EventHub, EventGrid, Logic, ApiManagement | 4 |
| security | KeyVault, ManagedIdentity | 5 |
| observability | Insights, OperationalInsights | 6 |

## Edge inference

An edge exists when resource A properties contain the resource ID of resource B in the same inventory. Label is the property key (`subnet`, `networkSecurityGroup`, `privateLinkServiceId`). Direction is referencer to referenced (consumer uses provider).

Always expand nested `virtualNetworks.properties.subnets` even if Resource Graph did not return subnet rows.

## Omit from the picture

- Locks, policy assignments, role assignments, deployment history, and diagnostic setting noise unless the user asked for governance.
- Individual disks when a VM is present, unless the user asked for storage layout. Mention disk count in the inventory.
- Metric alerts and action groups on the main diagram; list them under observability.

## Honesty rules

- Do not draw a line that is not in the export. App-level calls (a Function calling Storage by connection string without a resource ID) stay off the diagram and go in an "unverified dependencies" note.
- Unlinked resources are listed, not hidden.
- Private endpoints are their own nodes. Do not collapse them into the target PaaS service.
- Multi-region resources stay in one resource-group diagram; put location on the node label.

## Output files

| File | Use |
|---|---|
| diagram.mmd | Paste into Markdown |
| diagram.html | Open in a browser (needs Mermaid CDN) |
| diagram.drawio | Edit in diagrams.net |
| inventory.md | Name, type, location, edges, unlinked |
| model.json | Machine-readable nodes and edges |
