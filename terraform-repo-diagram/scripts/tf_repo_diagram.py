#!/usr/bin/env python3
"""Build an infrastructure diagram from Terraform in a GitHub repo.

Usage:
  python3 tf_repo_diagram.py --repo https://github.com/owner/repo
  python3 tf_repo_diagram.py --repo owner/repo --ref main --out ./out
  python3 tf_repo_diagram.py          # prompts for the target repo link
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

SKIP_DIRS = {".git", ".terraform", ".terragrunt-cache", "node_modules", "vendor"}
TF_EXTS = {".tf", ".tf.json"}

CATEGORY = [
    ("network", ("virtual_network", "subnet", "network_security", "route", "public_ip",
                 "lb", "application_gateway", "nat_gateway", "firewall", "dns",
                 "private_endpoint", "private_dns", "vpc", "subnet", "security_group",
                 "route_table", "internet_gateway", "nat_gateway", "load_balancer",
                 "lb_listener", "network_interface")),
    ("compute", ("virtual_machine", "linux_virtual_machine", "windows_virtual_machine",
                 "vmss", "aks", "kubernetes", "app_service", "function_app", "container",
                 "instance", "lambda", "ecs", "ec2", "compute")),
    ("data", ("storage", "sql", "cosmos", "redis", "database", "postgres", "mysql",
              "blob", "disk", "s3", "dynamodb", "rds")),
    ("security", ("key_vault", "role_assignment", "identity", "policy", "secret",
                  "kms", "iam", "waf")),
    ("integration", ("servicebus", "eventhub", "eventgrid", "api_management", "cdn",
                     "front_door", "sqs", "sns", "apigateway")),
]


def category_of(resource_type: str) -> str:
    tail = resource_type.split("_", 1)[-1].lower()
    for name, keys in CATEGORY:
        if any(k in tail or k in resource_type.lower() for k in keys):
            return name
    return "other"


def parse_repo(raw: str) -> tuple[str, str, str | None, str | None]:
    raw = raw.strip().rstrip("/")
    if raw.startswith("git@"):
        # git@github.com:owner/repo.git
        body = raw.split(":", 1)[-1]
        body = body[:-4] if body.endswith(".git") else body
        owner, repo = body.split("/", 1)
        return owner, repo, None, None
    if "://" not in raw and raw.count("/") == 1:
        owner, repo = raw.split("/", 1)
        return owner, repo.replace(".git", ""), None, None
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise SystemExit(f"Cannot parse repo link: {raw}")
    owner, repo = parts[0], parts[1].replace(".git", "")
    ref = None
    subpath = None
    if len(parts) >= 4 and parts[2] in {"tree", "blob"}:
        ref = parts[3]
        if len(parts) > 4:
            subpath = "/".join(parts[4:])
    return owner, repo, ref, subpath


def clone_repo(owner: str, repo: str, ref: str | None, dest: Path) -> None:
    url = f"https://github.com/{owner}/{repo}.git"
    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        cmd += ["--branch", ref]
    cmd += [url, str(dest)]
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise SystemExit(
            "Clone failed. Public repos work without a token. "
            "For private repos set GITHUB_TOKEN and retry.\n"
            + (proc.stderr or proc.stdout)
        )


def iter_tf_files(root: Path, subpath: str | None) -> list[Path]:
    base = root / subpath if subpath else root
    files = []
    for path in base.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in TF_EXTS and path.is_file():
            files.append(path)
    return sorted(files)


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)#.*?$", "", text)
    text = re.sub(r'(?m)(?<!\$)(?<![\\])//.*?$', "", text)
    return text


def blocks(text: str) -> list[tuple[str, str]]:
    """Return (header, body) for top-level HCL blocks."""
    text = strip_comments(text)
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        m = re.match(r"[A-Za-z0-9_.\"-]+(?:\s+[A-Za-z0-9_.\"-]+)*", text[i:])
        if not m:
            i += 1
            continue
        header = m.group(0).strip()
        j = i + m.end()
        while j < n and text[j].isspace():
            j += 1
        if j >= n or text[j] != "{":
            i = j
            continue
        depth = 0
        k = j
        while k < n:
            if text[k] == "{":
                depth += 1
            elif text[k] == "}":
                depth -= 1
                if depth == 0:
                    out.append((header, text[j + 1:k]))
                    i = k + 1
                    break
            k += 1
        else:
            break
    return out


ATTR_RE = re.compile(
    r'(?m)^\s*([A-Za-z0-9_]+)\s*=\s*("(?:\\.|[^"\\])*"|[^\n#]+)'
)
REF_RE = re.compile(
    r"\b((?:azurerm|aws|google|azapi)_[A-Za-z0-9_]+)\.([A-Za-z0-9_]+)"
)
MODULE_REF_RE = re.compile(r"\bmodule\.([A-Za-z0-9_]+)")


def unquote(value: str) -> str:
    value = value.strip().rstrip(",")
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def interesting_attrs(body: str) -> dict[str, str]:
    keep = {
        "name", "location", "resource_group_name", "sku_name", "sku",
        "account_tier", "account_replication_type", "address_space",
        "address_prefixes", "size", "vm_size", "tier", "capacity",
        "os_type", "kind", "storage_account_type", "administrator_login",
        "source", "version",
    }
    found = {}
    for key, raw in ATTR_RE.findall(body):
        if key in keep:
            found[key] = unquote(raw)[:120]
    return found


def parse_files(files: list[Path], root: Path) -> dict:
    resources = []
    modules = []
    providers = []
    variables = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = str(path.relative_to(root))
        for header, body in blocks(text):
            parts = header.split()
            kind = parts[0]
            if kind == "resource" and len(parts) >= 3:
                rtype = unquote(parts[1])
                rname = unquote(parts[2])
                refs = sorted({f"{a}.{b}" for a, b in REF_RE.findall(body)})
                mod_refs = sorted(set(MODULE_REF_RE.findall(body)))
                resources.append({
                    "id": f"{rtype}.{rname}",
                    "type": rtype,
                    "name": rname,
                    "category": category_of(rtype),
                    "file": rel,
                    "attrs": interesting_attrs(body),
                    "refs": refs,
                    "module_refs": mod_refs,
                })
            elif kind == "module" and len(parts) >= 2:
                mname = unquote(parts[1])
                attrs = interesting_attrs(body)
                refs = sorted({f"{a}.{b}" for a, b in REF_RE.findall(body)})
                modules.append({
                    "id": f"module.{mname}",
                    "name": mname,
                    "source": attrs.get("source", ""),
                    "file": rel,
                    "attrs": attrs,
                    "refs": refs,
                })
            elif kind == "provider" and len(parts) >= 2:
                providers.append({"name": unquote(parts[1]), "file": rel, "attrs": interesting_attrs(body)})
            elif kind == "variable" and len(parts) >= 2:
                variables.append(unquote(parts[1]))
    return {
        "resources": resources,
        "modules": modules,
        "providers": providers,
        "variables": variables,
    }


def edges_of(model: dict) -> list[dict]:
    by_id = {r["id"]: r for r in model["resources"]}
    mod_ids = {m["id"] for m in model["modules"]}
    edges = []
    seen = set()
    for node in model["resources"] + model["modules"]:
        for ref in node.get("refs", []):
            if ref in by_id and ref != node["id"]:
                key = (node["id"], ref)
                if key not in seen:
                    seen.add(key)
                    edges.append({"from": node["id"], "to": ref, "kind": "reference"})
        for mname in node.get("module_refs", []):
            mid = f"module.{mname}"
            if mid in mod_ids:
                key = (node["id"], mid)
                if key not in seen:
                    seen.add(key)
                    edges.append({"from": node["id"], "to": mid, "kind": "module"})
    return edges


def label(node: dict) -> str:
    attrs = node.get("attrs") or {}
    bits = [node["id"]]
    if attrs.get("name"):
        bits.append(attrs["name"])
    if attrs.get("location"):
        bits.append(attrs["location"])
    if attrs.get("sku_name") or attrs.get("sku") or attrs.get("vm_size") or attrs.get("size"):
        bits.append(attrs.get("sku_name") or attrs.get("sku") or attrs.get("vm_size") or attrs.get("size"))
    if attrs.get("account_tier"):
        bits.append(f"{attrs['account_tier']}/{attrs.get('account_replication_type', '')}".rstrip("/"))
    if attrs.get("source"):
        bits.append(attrs["source"])
    return " | ".join(bits)


def write_inventory(model: dict, edges: list[dict], out: Path, title: str) -> None:
    lines = [f"# {title}", "", f"- Providers: {', '.join(p['name'] for p in model['providers']) or 'none declared'}",
             f"- Resources: {len(model['resources'])}",
             f"- Modules: {len(model['modules'])}",
             f"- Reference edges: {len(edges)}", ""]
    lines.append("| Resource | Category | Config | File |")
    lines.append("|---|---|---|---|")
    for r in model["resources"]:
        cfg = ", ".join(f"{k}={v}" for k, v in r["attrs"].items()) or "-"
        lines.append(f"| `{r['id']}` | {r['category']} | {cfg} | `{r['file']}` |")
    if model["modules"]:
        lines += ["", "## Modules", ""]
        for m in model["modules"]:
            lines.append(f"- `{m['id']}` source=`{m['source']}` file=`{m['file']}`")
    if edges:
        lines += ["", "## Edges", ""]
        for e in edges:
            lines.append(f"- `{e['from']}` → `{e['to']}`")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


ICON_BASE = "https://cdn.jsdelivr.net/gh/jgraph/drawio@dev/src/main/webapp/img/lib/azure2"
ICONS = {
    "azurerm_resource_group": "general/Resource_Groups.svg",
    "azurerm_storage_account": "storage/Storage_Accounts.svg",
    "azurerm_storage_container": "storage/Storage_Accounts.svg",
    "azurerm_virtual_network": "networking/Virtual_Networks.svg",
    "azurerm_subnet": "networking/Subnet.svg",
    "azurerm_network_security_group": "networking/Network_Security_Groups.svg",
    "azurerm_public_ip": "networking/Public_IP_Addresses.svg",
    "azurerm_lb": "networking/Load_Balancers.svg",
    "azurerm_network_interface": "networking/Network_Interfaces.svg",
    "azurerm_private_endpoint": "networking/Private_Endpoint.svg",
    "azurerm_linux_virtual_machine": "compute/Virtual_Machine.svg",
    "azurerm_windows_virtual_machine": "compute/Virtual_Machine.svg",
    "azurerm_virtual_machine": "compute/Virtual_Machine.svg",
    "azurerm_key_vault": "security/Key_Vaults.svg",
    "azurerm_mssql_server": "databases/SQL_Server.svg",
    "azurerm_kubernetes_cluster": "compute/Kubernetes_Services.svg",
    "azurerm_app_service": "app%20services/App_Services.svg",
    "azurerm_linux_web_app": "app%20services/App_Services.svg",
    "azurerm_function_app": "compute/Function_Apps.svg",
}
CATEGORY_ICON = {
    "network": "networking/Virtual_Networks.svg",
    "compute": "compute/Virtual_Machine.svg",
    "data": "storage/Storage_Accounts.svg",
    "security": "security/Key_Vaults.svg",
    "integration": "integration/Service_Bus.svg",
    "module": "general/Resource_Groups.svg",
    "other": "general/Resource_Groups.svg",
}


def icon_for(node: dict) -> str:
    path = ICONS.get(node.get("type") or "") or CATEGORY_ICON.get(node.get("category"), CATEGORY_ICON["other"])
    return f"{ICON_BASE}/{path}"


def write_html(model: dict, edges: list[dict], out: Path, title: str) -> None:
    groups = defaultdict(list)
    for r in model["resources"]:
        cat = "foundation" if r["type"].endswith("resource_group") else r["category"]
        groups[cat].append(r)
    for m in model["modules"]:
        groups["module"].append({**m, "category": "module", "type": "module"})
    order = ["foundation", "network", "compute", "data", "security", "integration", "module", "other"]
    accents = {
        "foundation": "#0078D4", "network": "#5C2D91", "compute": "#0078D4", "data": "#008272",
        "security": "#D83B01", "integration": "#0078D4", "module": "#5E5E5E", "other": "#605E5C",
    }
    col_w, box_h, gap_x, gap_y, pad_x, pad_y = 280, 196, 70, 28, 36, 78
    positions = {}
    nodes = []
    x = pad_x
    max_rows = 1
    for cat in order:
        items = groups.get(cat) or []
        if not items:
            continue
        y = pad_y
        for item in items:
            positions[item["id"]] = (x, y, cat)
            nodes.append(item)
            y += box_h + gap_y
        max_rows = max(max_rows, len(items))
        x += col_w + gap_x
    width = max(x + pad_x - gap_x, 720)
    height = pad_y + max_rows * (box_h + gap_y) + 24

    def wrap(value: str, limit: int = 36) -> list[str]:
        words = value.split()
        lines, cur = [], ""
        for word in words:
            trial = word if not cur else cur + " " + word
            if len(trial) <= limit:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines[:3]

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<defs><marker id='arrow' markerWidth='8' markerHeight='8' refX='7' refY='3' orient='auto'><path d='M0,0 L7,3 L0,6' fill='#0078D4'/></marker></defs>",
        f"<rect width='{width}' height='{height}' fill='#ffffff'/>",
    ]
    for e in edges:
        if e["from"] not in positions or e["to"] not in positions:
            continue
        x1, y1, _ = positions[e["from"]]
        x2, y2, _ = positions[e["to"]]
        # connect icon centers, not the text block
        sx, sy = x1 + col_w - 16, y1 + 48
        tx, ty = x2 + 16, y2 + 48
        mid = (sx + tx) / 2
        parts.append(
            f"<path d='M {sx} {sy} C {mid} {sy}, {mid} {ty}, {tx} {ty}' fill='none' stroke='#0078D4' stroke-width='1.6' marker-end='url(#arrow)'/>"
        )
    for item in nodes:
        bx, by, cat = positions[item["id"]]
        attrs = item.get("attrs") or {}
        name = attrs.get("name") or item["id"]
        config = [f"{k}: {v}" for k, v in list(attrs.items())[:3] if k != "name"]
        parts.append(f"<rect x='{bx}' y='{by}' width='{col_w}' height='{box_h}' rx='10' fill='#f3f2f1' stroke='{accents.get(cat, '#0078D4')}' stroke-width='1.5'/>")
        parts.append(f"<image href='{html.escape(icon_for(item))}' x='{bx + (col_w - 56) / 2}' y='{by + 16}' width='56' height='56'/>")
        parts.append(f"<text x='{bx + col_w / 2}' y='{by + 92}' text-anchor='middle' font-family='Segoe UI, Arial, sans-serif' font-size='14' font-weight='600' fill='#201f1e'>{html.escape(name)}</text>")
        parts.append(f"<text x='{bx + col_w / 2}' y='{by + 112}' text-anchor='middle' font-family='Segoe UI, Arial, sans-serif' font-size='11' fill='#605e5c'>{html.escape(item['id'])}</text>")
        ty = by + 134
        for line in config:
            for piece in wrap(line, 38):
                parts.append(f"<text x='{bx + col_w / 2}' y='{ty}' text-anchor='middle' font-family='Segoe UI, Arial, sans-serif' font-size='11' fill='#323130'>{html.escape(piece)}</text>")
                ty += 15
    parts.append("</svg>")
    svg = "".join(parts)
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
body {{ margin: 0; font-family: Segoe UI, Arial, sans-serif; background: #f3f2f1; color: #201f1e; }}
header {{ padding: 20px 28px 0; }}
h1 {{ margin: 0; color: #0078d4; font-size: 22px; }}
p {{ margin: 6px 0 12px; color: #605e5c; }}
</style></head>
<body>
<header>
  <h1>{html.escape(title)}</h1>
  <p>Azure architecture icons sit above each resource name. Lines are Terraform references, not guessed traffic.</p>
</header>
{svg}
</body></html>"""
    out.write_text(page, encoding="utf-8")
    out.with_suffix(".svg").write_text(svg, encoding="utf-8")




ARM = {
    "azurerm_resource_group": "Microsoft.Resources/resourceGroups",
    "azurerm_storage_account": "Microsoft.Storage/storageAccounts",
    "azurerm_virtual_network": "Microsoft.Network/virtualNetworks",
    "azurerm_subnet": "Microsoft.Network/subnets",
    "azurerm_network_security_group": "Microsoft.Network/networkSecurityGroups",
    "azurerm_public_ip": "Microsoft.Network/publicIPAddresses",
    "azurerm_lb": "Microsoft.Network/loadBalancers",
    "azurerm_network_interface": "Microsoft.Network/networkInterfaces",
    "azurerm_private_endpoint": "Microsoft.Network/privateEndpoints",
    "azurerm_linux_virtual_machine": "Microsoft.Compute/virtualMachines",
    "azurerm_windows_virtual_machine": "Microsoft.Compute/virtualMachines",
    "azurerm_virtual_machine": "Microsoft.Compute/virtualMachines",
    "azurerm_key_vault": "Microsoft.KeyVault/vaults",
    "azurerm_mssql_server": "Microsoft.Sql/servers",
    "azurerm_kubernetes_cluster": "Microsoft.ContainerService/managedClusters",
}


def arm_type(tf_type: str) -> str:
    if tf_type in ARM:
        return ARM[tf_type]
    tail = tf_type.split("_", 1)[-1]
    return "Microsoft.Resources/" + tail


def dashboard_payload(model: dict, edges: list[dict]) -> dict:
    groups = {r["name"]: r["attrs"].get("name", r["name"]) for r in model["resources"] if r["type"].endswith("resource_group")}
    rows = []
    for r in model["resources"]:
        attrs = r.get("attrs") or {}
        rg = attrs.get("resource_group_name") or next(iter(groups.values()), "unknown")
        if rg.startswith("azurerm_"):
            rg = groups.get(rg.split(".")[1], rg)
        name = attrs.get("name") or r["name"]
        rid = f"/subscriptions/terraform/resourceGroups/{rg}"
        if not r["type"].endswith("resource_group"):
            rid += f"/providers/{arm_type(r['type'])}/{name}"
        props = {k: v for k, v in attrs.items() if k not in {"resource_group_name"}}
        if r["type"].endswith("storage_account") and "publicNetworkAccess" not in props:
            props["publicNetworkAccess"] = "Enabled"
        rows.append({
            "id": rid,
            "name": name,
            "type": arm_type(r["type"]),
            "location": attrs.get("location", ""),
            "resourceGroup": rg,
            "properties": props,
            "terraform": r["id"],
            "file": r["file"],
        })
    return {"data": rows, "source": model.get("repo"), "files": model.get("files", [])}


def write_dashboard(model: dict, edges: list[dict], out: Path, title: str) -> None:
    template = Path("/workspace/artifacts/azure-dashboard.html")
    if not template.exists():
        template = Path(__file__).resolve().parents[1] / "azure-dashboard" / "index.html"
    page = template.read_text(encoding="utf-8")
    payload = json.dumps(dashboard_payload(model, edges))
    page = page.replace("load(SAMPLE, \"rg-prod sample\");", f"load({payload}, {json.dumps(title)});")
    page = page.replace("<title>Azure resource dashboard</title>", f"<title>{html.escape(title)} dashboard</title>")
    page = page.replace("<h1>Azure resource dashboard</h1>", f"<h1>{html.escape(title)}</h1>")
    out.write_text(page, encoding="utf-8")


def build(repo_link: str, ref: str | None, out_dir: Path, title: str | None) -> dict:
    owner, repo, url_ref, subpath = parse_repo(repo_link)
    ref = ref or url_ref
    title = title or f"{owner}/{repo}"
    tmp = Path(tempfile.mkdtemp(prefix="tfdiag-"))
    try:
        clone_repo(owner, repo, ref, tmp)
        files = iter_tf_files(tmp, subpath)
        if not files:
            raise SystemExit(f"No .tf files found in {owner}/{repo}" + (f" at {subpath}" if subpath else ""))
        model = parse_files(files, tmp)
        model["repo"] = f"{owner}/{repo}"
        model["ref"] = ref or "default"
        model["files"] = [str(p.relative_to(tmp)) for p in files]
        edges = edges_of(model)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "model.json").write_text(json.dumps({"model": model, "edges": edges}, indent=2), encoding="utf-8")
        write_inventory(model, edges, out_dir / "inventory.md", title)
        write_html(model, edges, out_dir / "diagram.html", title)
        write_dashboard(model, edges, out_dir / "dashboard.html", title)
        return {"title": title, "resources": len(model["resources"]), "modules": len(model["modules"]),
                "edges": len(edges), "files": len(files), "out": str(out_dir)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagram Terraform from a GitHub repo link")
    parser.add_argument("--repo", help="Target repo link, e.g. https://github.com/owner/repo")
    parser.add_argument("--ref", help="Branch, tag, or commit")
    parser.add_argument("--out", default="./tf-diagram-out", help="Output directory")
    parser.add_argument("--title", help="Diagram title")
    args = parser.parse_args()
    repo = args.repo
    if not repo:
        repo = input("Target GitHub repo link: ").strip()
    if not repo:
        raise SystemExit("A target repo link is required.")
    summary = build(repo, args.ref, Path(args.out), args.title)
    print(json.dumps(summary, indent=2))
    print(f"Open {Path(args.out) / 'diagram.html'}")


if __name__ == "__main__":
    main()
