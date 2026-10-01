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
        groups[r["category"]].append(r)
    for m in model["modules"]:
        groups["module"].append({**m, "category": "module", "type": "module"})
    order = ["network", "compute", "data", "security", "integration", "module", "other"]
    accents = {
        "network": "#5C2D91", "compute": "#0078D4", "data": "#008272",
        "security": "#D83B01", "integration": "#0078D4", "module": "#5E5E5E", "other": "#0078D4",
    }
    sections = []
    id_map = {}
    n = 0
    for cat in order:
        items = groups.get(cat) or []
        if not items:
            continue
        cards = []
        for item in items:
            nid = f"n{n}"
            id_map[item["id"]] = nid
            n += 1
            attrs = item.get("attrs") or {}
            detail = html.escape(" · ".join(f"{k}={v}" for k, v in list(attrs.items())[:3]) or item.get("file", ""))
            cards.append(
                f'<article class="card" id="{nid}" data-node="{nid}">'
                f'<img alt="" src="{html.escape(icon_for(item))}"/>'
                f'<strong>{html.escape(item["id"])}</strong>'
                f'<span>{detail}</span></article>'
            )
        sections.append(
            f'<section class="col" style="--accent:{accents[cat]}"><h2>{html.escape(cat)}</h2>{"".join(cards)}</section>'
        )
    edge_js = json.dumps([
        {"from": id_map[e["from"]], "to": id_map[e["to"]]}
        for e in edges if e["from"] in id_map and e["to"] in id_map
    ])
    page = f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
body {{ margin: 0; font-family: "Segoe UI", sans-serif; background: #f3f2f1; color: #201f1e; }}
header {{ padding: 20px 28px 8px; }}
h1 {{ margin: 0; color: #0078d4; font-size: 22px; }}
p {{ margin: 6px 0 0; color: #605e5c; }}
#board {{ position: relative; margin: 16px; padding: 18px; background: #fff; border: 2px solid #0078d4; border-radius: 12px; }}
.cols {{ display: flex; gap: 28px; align-items: flex-start; position: relative; z-index: 1; }}
.col {{ min-width: 220px; }}
.col h2 {{ margin: 0 0 12px; font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: var(--accent); }}
.card {{ width: 200px; margin: 0 0 22px; text-align: center; }}
.card img {{ width: 52px; height: 52px; }}
.card strong, .card span {{ display: block; }}
.card strong {{ font-size: 13px; }}
.card span {{ font-size: 11px; color: #605e5c; }}
svg.links {{ position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }}
</style></head>
<body>
<header>
  <h1>{html.escape(title)}</h1>
  <p>Azure architecture icons. {len(model["resources"])} resources · {len(model["modules"])} modules · {len(edges)} reference edges. Lines are Terraform references, not guessed traffic.</p>
</header>
<div id="board">
  <svg class="links" id="links"></svg>
  <div class="cols">{"".join(sections)}</div>
</div>
<script>
const edges = {edge_js};
function draw() {{
  const board = document.getElementById("board");
  const svg = document.getElementById("links");
  const rect = board.getBoundingClientRect();
  svg.setAttribute("viewBox", `0 0 ${{board.clientWidth}} ${{board.clientHeight}}`);
  svg.innerHTML = '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6" fill="#0078D4"/></marker></defs>';
  for (const edge of edges) {{
    const a = document.getElementById(edge.from).getBoundingClientRect();
    const b = document.getElementById(edge.to).getBoundingClientRect();
    const x1 = a.left + a.width / 2 - rect.left;
    const y1 = a.top + 26 - rect.top;
    const x2 = b.left + b.width / 2 - rect.left;
    const y2 = b.top + 26 - rect.top;
    const mid = (x1 + x2) / 2;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", `M ${{x1}} ${{y1}} C ${{mid}} ${{y1}}, ${{mid}} ${{y2}}, ${{x2}} ${{y2}}`);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "#0078D4");
    path.setAttribute("stroke-width", "1.4");
    path.setAttribute("marker-end", "url(#arrow)");
    svg.appendChild(path);
  }}
}}
window.addEventListener("load", draw);
window.addEventListener("resize", draw);
</script>
</body></html>'''
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
