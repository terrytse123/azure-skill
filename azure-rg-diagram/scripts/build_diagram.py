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

ICON_BASE = "https://cdn.jsdelivr.net/gh/jgraph/drawio@dev/src/main/webapp/img/lib/azure2"
ICONS = {
    "virtualnetworks": "networking/Virtual_Networks.svg",
    "subnets": "networking/Subnet.svg",
    "networksecuritygroups": "networking/Network_Security_Groups.svg",
    "publicipaddresses": "networking/Public_IP_Addresses.svg",
    "loadbalancers": "networking/Load_Balancers.svg",
    "applicationgateways": "networking/Application_Gateways.svg",
    "networkinterfaces": "networking/Network_Interfaces.svg",
    "privateendpoints": "networking/Private_Endpoint.svg",
    "privatednszones": "networking/DNS_Zones.svg",
    "firewalls": "networking/Firewalls.svg",
    "routetables": "networking/Route_Tables.svg",
    "natgateways": "networking/NAT.svg",
    "bastionhosts": "networking/Bastions.svg",
    "frontdoors": "networking/Front_Doors.svg",
    "virtualmachines": "compute/Virtual_Machine.svg",
    "virtualmachinescalesets": "compute/VM_Scale_Sets.svg",
    "disks": "compute/Disks.svg",
    "availabilitysets": "compute/Availability_Sets.svg",
    "managedclusters": "containers/Kubernetes_Services.svg",
    "sites": "app_services/App_Services.svg",
    "serverfarms": "app_services/App_Service_Plans.svg",
    "functions": "compute/Function_Apps.svg",
    "containerapps": "containers/Container_Apps.svg",
    "servers": "databases/SQL_Server.svg",
    "databases": "databases/SQL_Database.svg",
    "storageaccounts": "storage/Storage_Accounts.svg",
    "vaults": "security/Key_Vaults.svg",
    "userassignedidentities": "identity/Managed_Identities.svg",
    "workspaces": "management_governance/Log_Analytics_Workspaces.svg",
    "namespaces": "integration/Service_Bus.svg",
    "components": "management_governance/Application_Insights.svg",
}
CATEGORY_ICON = {
    "network": "networking/Virtual_Networks.svg",
    "compute": "compute/Virtual_Machine.svg",
    "data": "databases/SQL_Database.svg",
    "integration": "integration/Service_Bus.svg",
    "security": "security/Key_Vaults.svg",
    "observability": "management_governance/Log_Analytics_Workspaces.svg",
    "other": "general/All_Resources.svg",
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


def icon_for(resource: dict) -> str:
    key = short_type(resource.get("type") or "").lower()
    relative = ICONS.get(key) or CATEGORY_ICON.get(resource.get("category") or "other")
    return f"{ICON_BASE}/{relative}"


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
    width = max(980, 230 * max(len(order), 1) + 80)
    height = 160 + 120 * max((len(v) for v in grouped.values()), default=1)
    cells.append(
        f'<mxCell id="rg" value="{html.escape(title)}" style="rounded=1;whiteSpace=wrap;html=1;fillColor=#F3F2F1;strokeColor=#0078D4;strokeWidth=2;fontStyle=1;verticalAlign=top;spacingTop=8;fontSize=16;fontColor=#0078D4;" vertex="1" parent="1">'
        f'<mxGeometry x="20" y="20" width="{width}" height="{height + 40}" as="geometry"/></mxCell>'
    )
    for col, cat in enumerate(order):
        cells.append(
            f'<mxCell id="cat_{cat}" value="{cat}" style="text;html=1;fontStyle=1;fontSize=13;fontColor={FILL[cat]};" vertex="1" parent="rg">'
            f'<mxGeometry x="{36 + col * 220}" y="36" width="180" height="24" as="geometry"/></mxCell>'
        )
        for row, item in enumerate(grouped[cat]):
            x = 46 + col * 220
            y = 72 + row * 118
            icon = html.escape(icon_for(item))
            style = (
                "shape=image;html=1;verticalLabelPosition=bottom;verticalAlign=top;imageAspect=1;"
                f"aspect=fixed;image={icon};fontSize=11;fontColor=#201F1E;"
            )
            value = html.escape(f"{item['name']}\n{short_type(item['type'])}")
            cells.append(
                f'<mxCell id="{item["node"]}" value="{value}" style="{style}" vertex="1" parent="rg">'
                f'<mxGeometry x="{x}" y="{y}" width="56" height="56" as="geometry"/></mxCell>'
            )
    for index, edge in enumerate(edges):
        src = by_id[edge["from"]]["node"]
        dst = by_id[edge["to"]]["node"]
        label = html.escape(edge["label"])
        cells.append(
            f'<mxCell id="e{index}" value="{label}" style="edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;strokeColor=#0078D4;fontSize=10;fontColor=#605E5C;" edge="1" parent="rg" source="{src}" target="{dst}">'
            '<mxGeometry relative="1" as="geometry"/></mxCell>'
        )
    body = "\n        ".join(cells)
    return f"""<mxfile host="app.diagrams.net">
  <diagram name="{html.escape(title)}" id="azure-rg">
    <mxGraphModel dx="1400" dy="900" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="1600" pageHeight="1200">
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


def html_page(title: str, resources: list[dict], edges: list[dict]) -> str:
    by_id = {item["id"]: item for item in resources}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in resources:
        grouped[item["category"]].append(item)
    order = [c for c in ["network", "compute", "data", "integration", "security", "observability", "other"] if grouped.get(c)]
    columns = []
    for cat in order:
        cards = []
        for item in grouped[cat]:
            cards.append(
                f'<article class="card" id="{item["node"]}" data-node="{item["node"]}">'
                f'<img alt="" src="{html.escape(icon_for(item))}"/>'
                f'<strong>{html.escape(item["name"])}</strong>'
                f'<span>{html.escape(short_type(item["type"]))}</span>'
                f'</article>'
            )
        columns.append(
            f'<section class="col" style="--accent:{FILL[cat]}"><h2>{html.escape(cat)}</h2>{"".join(cards)}</section>'
        )
    edge_json = json.dumps([{"from": by_id[e["from"]]["node"], "to": by_id[e["to"]]["node"], "label": e["label"]} for e in edges])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <style>
    body {{ margin: 0; font-family: "Segoe UI", sans-serif; background: #f3f2f1; color: #201f1e; }}
    header {{ padding: 20px 28px 8px; }}
    h1 {{ margin: 0; color: #0078d4; font-size: 22px; }}
    p {{ margin: 6px 0 0; color: #605e5c; }}
    #board {{ position: relative; margin: 16px; padding: 18px; background: #fff; border: 2px solid #0078d4; border-radius: 12px; }}
    .cols {{ display: flex; gap: 28px; align-items: flex-start; position: relative; z-index: 1; }}
    .col {{ min-width: 180px; }}
    .col h2 {{ margin: 0 0 12px; font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: var(--accent); }}
    .card {{ width: 148px; margin: 0 0 22px; text-align: center; }}
    .card img {{ width: 52px; height: 52px; }}
    .card strong, .card span {{ display: block; }}
    .card strong {{ font-size: 13px; }}
    .card span {{ font-size: 11px; color: #605e5c; }}
    svg.links {{ position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(title)}</h1>
    <p>Azure resource group. Icons are the Azure architecture set. Lines are resource-id references, not guessed traffic.</p>
  </header>
  <div id="board">
    <svg class="links" id="links"></svg>
    <div class="cols">{"".join(columns)}</div>
  </div>
  <script>
    const edges = {edge_json};
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
    (out / "diagram.drawio").write_text(drawio(resources, edges, title))
    (out / "diagram.html").write_text(html_page(title, resources, edges))
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
