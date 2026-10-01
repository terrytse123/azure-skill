# rg-prod

Resources: 11  |  Relationships: 9

## network

| Name | Type | Location |
|---|---|---|
| vnet-app | `Microsoft.Network/virtualNetworks` | eastus |
| nsg-web | `Microsoft.Network/networkSecurityGroups` | eastus |
| pip-lb | `Microsoft.Network/publicIPAddresses` | eastus |
| lb-web | `Microsoft.Network/loadBalancers` | eastus |
| nic-web-01 | `Microsoft.Network/networkInterfaces` | eastus |
| pe-sql | `Microsoft.Network/privateEndpoints` | eastus |
| snet-web | `Microsoft.Network/virtualNetworks/subnets` | eastus |
| snet-data | `Microsoft.Network/virtualNetworks/subnets` | eastus |

## compute

| Name | Type | Location |
|---|---|---|
| vm-web-01 | `Microsoft.Compute/virtualMachines` | eastus |

## data

| Name | Type | Location |
|---|---|---|
| sql-app | `Microsoft.Sql/servers` | eastus |

## security

| Name | Type | Location |
|---|---|---|
| kv-app | `Microsoft.KeyVault/vaults` | eastus |

## relationships

- vnet-app --subnets--> snet-web
- vnet-app --network security group--> nsg-web
- vnet-app --subnets--> snet-data
- lb-web --public ipaddress--> pip-lb
- nic-web-01 --subnet--> snet-web
- vm-web-01 --network interfaces--> nic-web-01
- pe-sql --subnet--> snet-data
- pe-sql --private link service id--> sql-app
- snet-web --network security group--> nsg-web

## unlinked

No resource-id reference found. Confirm with a portal check or a deeper GET before treating these as isolated.

- kv-app
