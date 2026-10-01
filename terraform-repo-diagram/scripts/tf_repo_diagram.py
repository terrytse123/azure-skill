#!/usr/bin/env python3
"""Build an infrastructure diagram from Terraform in a GitHub repo.

The script stays in this repo. Pass a target repo link each run.

Usage:
  python3 terraform-repo-diagram/scripts/tf_repo_diagram.py --repo https://github.com/owner/repo
  python3 terraform-repo-diagram/scripts/tf_repo_diagram.py --repo owner/repo --ref main --out ./out
  python3 terraform-repo-diagram/scripts/tf_repo_diagram.py
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

SKIP_DIRS = {".git", ".terraform", ".terragrunt-cache", "node_modules", "vendor"}
TF_EXTS = {".tf", ".tf.json"}

CATEGORY = [
    ("network", ("virtual_network", "subnet", "network_security", "route", "public_ip",
                 "lb", "application_gateway", "nat_gateway", "firewall", "dns",
                 "private_endpoint", "private_dns", "vpc", "security_group",
                 "route_table", "internet_gateway", "load_balancer",
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
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        cmd += ["--branch", ref]
    cmd += [url, str(dest)]
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        err = proc.stderr or proc.stdout
        err = err.replace(token, "***") if token else err
        raise SystemExit(
            "Clone failed. Public repos work without a token. "
            "For private repos set GITHUB_TOKEN and retry.\n" + err
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
    text = re.sub(r"(?m)(?<!\$)(?<![\\])//.*?$", "", text)
    return text


def blocks(text: str) -> list[tuple[str, str]]:
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


def write_inventory(model: dict, edges: list[dict], out: Path, title: str) -> None:
    lines = [
        f"# {title}",
        "",
        f"- Providers: {', '.join(p['name'] for p in model['providers']) or 'none declared'}",
        f"- Resources: {len(model['resources'])}",
        f"- Modules: {len(model['modules'])}",
        f"- Reference edges: {len(edges)}",
        "",
        "| Resource | Category | Config | File |",
        "|---|---|---|---|",
    ]
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
            lines.append(f"- `{e['from']}` \u2192 `{e['to']}`")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_html(model: dict, edges: list[dict], out: Path, title: str) -> None:
    groups = defaultdict(list)
    for r in model["resources"]:
        groups[r["category"]].append(r)
    for m in model["modules"]:
        groups["module"].append({**m, "category": "module", "type": "module"})
    order = ["network", "compute", "data", "security", "integration", "module", "other"]
    colors = {
        "network": "#d6e8ff", "compute": "#d9f2e6", "data": "#fff1cc",
        "security": "#fde2e2", "integration": "#efe4ff", "module": "#e7e7e7", "other": "#f4f4f4",
    }
    col_w, box_h, gap_y, pad = 300, 92, 16, 24
    nodes = []
    positions = {}
    x = pad
    max_rows = 1
    for cat in order:
        items = groups.get(cat) or []
        if not items:
            continue
        y = 70
        for item in items:
            positions[item["id"]] = (x, y)
            nodes.append((item, x, y, colors.get(cat, "#f4f4f4"), cat))
            y += box_h + gap_y
        max_rows = max(max_rows, len(items))
        x += col_w + 48
    width = max(x + pad, 640)
    height = 90 + max_rows * (box_h + gap_y) + 40

    edge_svg = []
    for e in edges:
        if e["from"] not in positions or e["to"] not in positions:
            continue
        x1, y1 = positions[e["from"]]
        x2, y2 = positions[e["to"]]
        edge_svg.append(
            f'<line x1="{x1 + col_w}" y1="{y1 + box_h / 2}" x2="{x2}" y2="{y2 + box_h / 2}" '
            'stroke="#5b6b7c" stroke-width="1.4" marker-end="url(#arrow)"/>'
        )
    box_svg = []
    for item, bx, by, fill, cat in nodes:
        attrs = item.get("attrs") or {}
        title_line = html.escape(item["id"])
        detail = html.escape(
            " \u00b7 ".join(f"{k}={v}" for k, v in list(attrs.items())[:4]) or item.get("file", "")
        )
        box_svg.append(
            f'<g><rect x="{bx}" y="{by}" width="{col_w}" height="{box_h}" rx="8" '
            f'fill="{fill}" stroke="#31475e"/>'
            f'<text x="{bx + 12}" y="{by + 22}" font-size="11" fill="#5b6b7c">{html.escape(cat)}</text>'
            f'<text x="{bx + 12}" y="{by + 44}" font-size="13" font-weight="600" fill="#1b2838">{title_line}</text>'
            f'<text x="{bx + 12}" y="{by + 66}" font-size="11" fill="#31475e">{detail}</text></g>'
        )
    page = f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font-family:Segoe UI,sans-serif;margin:24px;color:#1b2838}}
h1{{font-size:20px}} .meta{{color:#5b6b7c;margin-bottom:12px}}</style></head>
<body>
<h1>{html.escape(title)}</h1>
<p class="meta">{len(model["resources"])} resources \u00b7 {len(model["modules"])} modules \u00b7 {len(edges)} reference edges. Lines are Terraform references, not guessed traffic.</p>
<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6" fill="#5b6b7c"/></marker></defs>
{''.join(edge_svg)}
{''.join(box_svg)}
</svg>
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
        return {
            "title": title,
            "resources": len(model["resources"]),
            "modules": len(model["modules"]),
            "edges": len(edges),
            "files": len(files),
            "out": str(out_dir),
        }
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
