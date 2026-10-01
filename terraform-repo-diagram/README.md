# terraform-repo-diagram

The script lives here. You pass a target GitHub repo link. It clones that repo, reads `.tf` files, and writes a diagram with basic config on each resource.

It does not deploy anything. Lines are Terraform references (`azurerm_resource_group.example.name`), not guessed traffic.

## Run

```bash
python3 terraform-repo-diagram/scripts/tf_repo_diagram.py \
  --repo https://github.com/owner/name \
  --out ./tf-diagram-out
```

Also accepted:

- `owner/name`
- `https://github.com/owner/name/tree/main/project` (branch plus subpath)
- no `--repo`: the script prompts for the link

Private repos: `export GITHUB_TOKEN=...` then run the same command.

## Outputs

| File | Contents |
|---|---|
| `diagram.html` | Layered diagram with name, location, SKU, and other basic config |
| `inventory.md` | Resource table and reference edges |
| `model.json` | Parsed resources, modules, providers |

Open `diagram.html` in a browser.
