#!/usr/bin/env python3
"""Build an Azure resource-group infrastructure diagram from exported JSON.

Accepts Azure Resource Graph (`{"data": [...]}`), `az resource list` (array),
or an ARM export (`{"resources": [...]}`). Writes Mermaid, draw.io, HTML, and
an inventory markdown file.

Usage:
  python3 build_diagram.py --input resources.json --out ./diagram-out --title "rg-prod"
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ID_RE = re.compile(
    r"(?i)(/subscriptions/[^/\s\"']+/resourceGroups/[^/\s\"']+/providers/[A-Za-z0-9.]+(?:/[A-Za-z0-9._()-]+){2,})"
)

CATEGORIES = [
    ("network", ("microsoft.network/",)),
    (
        "compute",
        (
            "microsoft.compute/",
            "microsoft.containerservice/",
            "microsoft.web/",
            "microsoft.app/",
            "microsoft.containerinstance/",
            "microsoft.batch/",
        ),
    ),
    (
        "data",
        (
            "microsoft.sql/",
            "microsoft.documentdb/",
            "microsoft.dbfor",
            "microsoft.cache/",
            "microsoft.storage/",
            "microsoft.synapse/",
            "microsoft.dbformysql/",
            "microsoft.dbforpostgresql/",
        ),
    ),
    (
        "integration",
        (
            "microsoft.servicebus/",
            "microsoft.eventhub/",
            "microsoft.eventgrid/",
            "microsoft.logic/",
            "microsoft.apimanagement/",
            "microsoft.relay/",
            "microsoft.signalrservice/",
        ),
    ),
    ("security", ("microsoft.keyvault/", "microsoft.managedidentity/",)),
    (
        "observability",
        ("microsoft.insights/", "microsoft.operationalinsights/", "microsoft.monitor/"),
    ),
]

FILL = {
    "network": "#5C2D91",
    "compute": "#0078D4",
    "data": "#008272",
    "integration": "#C239B3",
    "security": "#D83B01",
    "observability": "#4A4A4A",
    "other": "#767676",
}

SKIP_EDGE_KEYS = {
    "id",
    "resourceid",
    "etag",
    "provisioningstate",
    "resourceguid",
    "type",
    "location",
}


def load_payload(path: Path) -> tuple[list[dict], str]:
    raw = json.loads(path.read_text())
    title = path.stem
    if isinstance(raw, list):
        return raw, title
    if isinstance(raw, dict):
        if isinstance(raw.get("data"), list):
            return raw["data"], title
        if isinstance(raw.get("resources"), list):
            return raw["resources"], raw.get("parameters", {}).get("resourceGroup", title) if False else title
        if isinstance(raw.get("value"), list):
            return raw["value"], title
    raise SystemExit("Unrecognized JSON. Expected a resource array, Resource Graph {data:[]}, or ARM {resources:[]}.")


def norm_id(value: str) -> str:
    return value.strip().rstrip("/").lower()


def short_type(resource_type: str) -> str:
    parts = resource_type.split("/")
    return parts[-1] if parts else resource_type


def category_of(resource_type: str) -> str:
    lowered = resource_type.lower()
    for name, prefixes in CATEGORIES:
        if lowered.startswith(prefixes):
            return name
    return "other"


def stable_node_id(index: int) -> str:
    return f"n{index}"


def label_for(name: str, resource_type: str) -> str:
    kind = short_type(resource_type)
    return f"{name}\\n{kind}"


def extract_id_strings(value, path: list[str], found: list[tuple[str, str]]):
    if isinstance(value, str):
        for match in ID_RE.findall(value):
            key = next((p for p in reversed(path) if p.lower() not in SKIP_EDGE_KEYS), "ref")
            found.append((match, key))
    elif isinstance(value, dict):
        for key, child in value.items():
            extract_id_strings(child, path + [str(key)], found)
    elif isinstance(value, list):
        for child in value:
            extract_id_strings(child, path, found)


def synthesize_id(resource: dict, index: int) -> str:
    existing = resource.get("id")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    resource_type = resource.get("type") or "Microsoft.Unknown/unknown"
    name = resource.get("name") or f"resource-{index}"
    rg = resource.get("resourceGroup") or "unknown"
    return f"/subscriptions/unknown/resourceGroups/{rg}/providers/{resource_type}/{name}"


def expand_nested(resources: list[dict]) -> list[dict]:
    """Surface VNet subnets that only appear nested under virtualNetworks."""
    known = {norm_id(r["id"]) for r in resources if r.get("id")}
    extra: list[dict] = []
    for resource in resources:
        rtype = (resource.get("type") or "").lower()
        if rtype != "microsoft.network/virtualnetworks":
            continue
        props = resource.get("properties") or {}
        for subnet in props.get("subnets") or []:
            sid = subnet.get("id")
            if not isinstance(sid, str) or norm_id(sid) in known:
                continue
            extra.append(
                {
                    "id": sid,
                    "name": subnet.get("name") or sid.rsplit("/", 1)[-1],
                    "type": "Microsoft.Network/virtualNetworks/subnets",
                    "location": resource.get("location"),
                    "resourceGroup": resource.get("resourceGroup"),
                    "properties": subnet.get("properties") or {},
                    "_synthetic": True,
                }
            )
            known.add(norm_id(sid))
    return resources + extra


def build_model(raw_resources: list[dict]) -> tuple[list[dict], list[dict]]:
    resources = []
    for index, resource in enumerate(raw_resources):
        if not isinstance(resource, dict):
            continue
        item = dict(resource)
        item["id"] = synthesize_id(item, index)
        item["name"] = item.get("name") or item["id"].rsplit("/", 1)[-1]
        item["type"] = item.get("type") or "Microsoft.Unknown/unknown"
        item["category"] = category_of(item["type"])
        resources.append(item)
    resources = expand_nested(resources)
    for item in resources:
        item["category"] = category_of(item.get("type") or "")

    by_id = {norm_id(item["id"]): item for item in resources}
    edges = []
    seen = set()
    for source in resources:
        refs: list[tuple[str, str]] = []
        extract_id_strings(source.get("properties") or {}, [], refs)
        for target_id, key in refs:
            target = by_id.get(norm_id(target_id))
            if not target or target["id"] == source["id"]:
                continue
            label = re.sub(r"([a-z])([A-Z])", r"\1 \2", key).lower()
            signature = (source["id"], target["id"], label)
            if signature in seen:
                continue
            seen.add(signature)
            edges.append({"from": source["id"], "to": target["id"], "label": label})
    for index, item in enumerate(resources):
        item["node"] = stable_node_id(index)
    return resources, edges


def mermaid(resources: list[dict], edges: list[dict], title: str) -> str:
    by_id = {item["id"]: item for item in resources}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in resources:
        grouped[item["category"]].append(item)
    lines = ["flowchart TB"]
    safe_title = title.replace('"', "'")
    lines.append(f'  title["{safe_title}"]')
    order = ["network", "compute", "data", "integration", "security", "observability", "other"]
    for cat in order:
        items = grouped.get(cat) or []
        if not items:
            continue
        lines.append(f"  subgraph {cat}[{cat}]")
        for item in items:
            text = f"{item['name']}\\n{short_type(item['type'])}"
            text = text.replace('"', "'")
            lines.append(f'    {item["node"]}["{text}"]')
        lines.append("  end")
    for edge in edges:
        src = by_id[edge["from"]]["node"]
        dst = by_id[edge["to"]]["node"]
        label = edge["label"].replace('"', "'")
        lines.append(f'  {src} -->|{label}| {dst}')
    return "\n".join(lines) + "\n"


def drawio(resources: list[dict], edges: list[dict], title: str) -> str:
    by_id = {item["id"]: item for item in resources}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in resources:
        grouped[item["category"]].append(item)
    order = [c for c in ["network", "compute", "data", "integration", "security", "observability", "other"] if grouped.get(c)]
    cells = [
        '<mxCell id="0"/>',
        '<mxCell id="1" parent="0"/>',
    ]
    width = max(900, 220 * max(len(order), 1) + 80)
    height = 120 + 90 * max((len(v) for v in grouped.values()), default=1)
    cells.append(
        f'<mxCell id="rg" value="{html.escape(title)}" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#F3F2F1;strokeColor=#605E5C;fontStyle=1;verticalAlign=top;spacingTop=8;" vertex="1" parent="1">'
        f'<mxGeometry x="20" y="20" width="{width}" height="{height + 60}" as="geometry"/></mxCell>'
    )
    node_pos = {}
    for col, cat in enumerate(order):
        cells.append(
            f'<mxCell id="cat_{cat}" value="{cat}" style="text;html=1;fontStyle=1;fontColor={FILL[cat]};" vertex="1" parent="rg">'
            f'<mxGeometry x="{40 + col * 210}" y="36" width="180" height="24" as="geometry"/></mxCell>'
        )
        for row, item in enumerate(grouped[cat]):
            x = 40 + col * 210
            y = 70 + row * 88
            node_pos[item["id"]] = (x, y)
            style = (
                f"rounded=1;whiteSpace=wrap;html=1;fillColor={FILL[cat]};fontColor=#FFFFFF;"
                "strokeColor=#201F1E;fontSize=11;"
            )
            value = html.escape(f"{item['name']}\n{short_type(item['type'])}")
            cells.append(
                f'<mxCell id="{item["node"]}" value="{value}" style="{style}" vertex="1" parent="rg">'
                f'<mxGeometry x="{x}" y="{y}" width="170" height="64" as="geometry"/></mxCell>'
            )
    for index, edge in enumerate(edges):
        src = by_id[edge["from"]]["node"]
        dst = by_id[edge["to"]]["node"]
        label = html.escape(edge["label"])
        cells.append(
            f'<mxCell id="e{index}" value="{label}" style="edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;strokeColor=#605E5C;fontSize=10;" edge="1" parent="rg" source="{src}" target="{dst}">'
            '<mxGeometry relative="1" as="geometry"/></mxCell>'
        )
    body = "\n        ".join(cells)
    return f"""<mxfile host="app.diagrams.net">
  <diagram name="{html.escape(title)}" id="azure-rg">
    <mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="1600" pageHeight="1200">
      <root>
        {body}
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
"""


def inventory(resources: list[dict], edges: list[dict], title: str) -> str:
    by_id = {item["id"]: item for item in resources}
    lines = [f"# {title}", "", f"Resources: {len(resources)}  |  Relationships: {len(edges)}", ""]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in resources:
        grouped[item["category"]].append(item)
    for cat in ["network", "compute", "data", "integration", "security", "observability", "other"]:
        items = grouped.get(cat) or []
        if not items:
            continue
        lines.append(f"## {cat}")
        lines.append("")
        lines.append("| Name | Type | Location |")
        lines.append("|---|---|---|")
        for item in items:
            loc = item.get("location") or ""
            lines.append(f"| {item['name']} | `{item['type']}` | {loc} |")
        lines.append("")
    if edges:
        lines.append("## relationships")
        lines.append("")
        for edge in edges:
            src = by_id[edge["from"]]["name"]
            dst = by_id[edge["to"]]["name"]
            lines.append(f"- {src} --{edge['label']}--> {dst}")
        lines.append("")
    orphans = [
        item["name"]
        for item in resources
        if item["id"] not in {e["from"] for e in edges} and item["id"] not in {e["to"] for e in edges}
    ]
    if orphans:
        lines.append("## unlinked")
        lines.append("")
        lines.append("No resource-id reference found. Confirm with a portal check or a deeper GET before treating these as isolated.")
        lines.append("")
        for name in orphans:
            lines.append(f"- {name}")
        lines.append("")
    return "\n".join(lines)


def html_page(title: str, mermaid_src: str) -> str:
    escaped = mermaid_src.replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
  <style>
    body {{ font-family: Segoe UI, sans-serif; margin: 24px; background: #faf9f8; color: #201f1e; }}
    h1 {{ font-size: 22px; }}
    .mermaid {{ background: white; border: 1px solid #e1dfdd; padding: 16px; }}
  </style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <p>Azure resource group infrastructure diagram. Open diagram.drawio in diagrams.net to edit.</p>
  <pre class="mermaid">{escaped}</pre>
  <script>mermaid.initialize({{ startOnLoad: true, theme: "default" }});</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an Azure RG infrastructure diagram")
    parser.add_argument("--input", required=True, help="Resource Graph, az resource list, or ARM JSON")
    parser.add_argument("--out", required=True, help="Output directory")
    parser.add_argument("--title", default="", help="Diagram title, usually the resource group name")
    args = parser.parse_args()

    source = Path(args.input)
    resources_raw, default_title = load_payload(source)
    title = args.title or default_title
    resources, edges = build_model(resources_raw)
    if not resources:
        print("No resources found in input.", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    mmd = mermaid(resources, edges, title)
    (out / "diagram.mmd").write_text(mmd)
    (out / "diagram.drawio").write_text(drawio(resources, edges, title))
    (out / "diagram.html").write_text(html_page(title, mmd))
    (out / "inventory.md").write_text(inventory(resources, edges, title))
    (out / "model.json").write_text(
        json.dumps(
            {
                "title": title,
                "resourceCount": len(resources),
                "edgeCount": len(edges),
                "resources": [
                    {"name": r["name"], "type": r["type"], "category": r["category"], "id": r["id"]}
                    for r in resources
                ],
                "edges": edges,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Wrote {len(resources)} resources and {len(edges)} edges to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
