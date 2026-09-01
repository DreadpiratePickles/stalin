"""`stalin mcp` — Model Context Protocol server on stdio. Read-only.

Minimal JSON-RPC 2.0; enough for Claude Code / any MCP client to list sources,
pull typed data, and read schemas. Write tools deliberately excluded in v1.
"""
from __future__ import annotations

import json
import sys

from .config import Project
from .schema import json_schema

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {"name": "list_sources",
     "description": "List all configured stalin sources with status and schema version.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_data",
     "description": ("Get the latest extracted data for a source as typed JSON "
                     "(cached last-good snapshot; includes fetched_at and stale flag)."),
     "inputSchema": {"type": "object",
                     "properties": {"source": {"type": "string"}},
                     "required": ["source"]}},
    {"name": "get_schema",
     "description": "Get the JSON Schema contract for a source.",
     "inputSchema": {"type": "object",
                     "properties": {"source": {"type": "string"}},
                     "required": ["source"]}},
]


def _tool_result(payload) -> dict:
    return {"content": [{"type": "text",
                         "text": json.dumps(payload, ensure_ascii=False, indent=1)}]}


def _lookup_tools(project: Project) -> list:
    """Each live_lookup source with expose_as_tool becomes a typed MCP tool."""
    tools = []
    for n in project.source_names():
        try:
            spec = project.load_source(n)
        except Exception:
            continue
        if not (spec.is_lookup and spec.expose_as_tool):
            continue
        props = {pn: {"type": "string", "description": pp.desc}
                 for pn, pp in spec.params.items()}
        required = [pn for pn, pp in spec.params.items() if pp.required]
        tools.append({
            "name": f"lookup_{n}",
            "description": (f"Live lookup of {n} by {', '.join(spec.params)}. "
                            f"Returns typed, self-healing JSON scraped on demand from "
                            f"{spec.url}."),
            "inputSchema": {"type": "object", "properties": props, "required": required},
        })
    return tools


def _call_tool(project: Project, name: str, args: dict) -> dict:
    if name.startswith("lookup_"):
        src = name[len("lookup_"):]
        if src not in project.source_names():
            return {"content": [{"type": "text", "text": f"unknown source {src!r}"}],
                    "isError": True}
        import asyncio
        from .lookup import run_lookup
        from .lockfile import Lock
        spec = project.load_source(src)
        lock = Lock.load(project.lock_path)
        lres = asyncio.run(run_lookup(project, spec, lock, args or {}))
        return _tool_result(lres.envelope or {"error": lres.error, "status": lres.status})
    if name == "list_sources":
        from .lockfile import Lock
        lock = Lock.load(project.lock_path)
        out = []
        for n in project.source_names():
            spec = project.load_source(n)
            sl = lock.sources.get(n)
            out.append({"name": n, "url": spec.url,
                        "schema_version": spec.contract.version,
                        "status": sl.status if sl else "uncompiled"})
        return _tool_result(out)
    src = args.get("source", "")
    if src not in project.source_names():
        return {"content": [{"type": "text",
                             "text": f"unknown source {src!r}"}], "isError": True}
    if name == "get_data":
        dp = project.data_path(src)
        if not dp.exists():
            return {"content": [{"type": "text",
                                 "text": f"no data yet — run `stalin run {src}`"}],
                    "isError": True}
        return _tool_result(json.loads(dp.read_text()))
    if name == "get_schema":
        spec = project.load_source(src)
        return _tool_result(json_schema(src, spec.ftypes, spec.contract.version))
    return {"content": [{"type": "text", "text": f"unknown tool {name!r}"}],
            "isError": True}


def serve_stdio(project: Project) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = req.get("id")
        method = req.get("method", "")
        if method == "initialize":
            resp = {"protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "stalin", "version": "0.1.0"}}
        elif method == "tools/list":
            resp = {"tools": TOOLS + _lookup_tools(project)}
        elif method == "tools/call":
            p = req.get("params", {})
            try:
                resp = _call_tool(project, p.get("name", ""),
                                  p.get("arguments", {}) or {})
            except Exception as e:
                resp = {"content": [{"type": "text", "text": f"error: {e}"}],
                        "isError": True}
        elif method.startswith("notifications/"):
            continue
        elif method == "ping":
            resp = {}
        else:
            if rid is not None:
                sys.stdout.write(json.dumps(
                    {"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32601, "message": f"unknown method {method}"}})
                    + "\n")
                sys.stdout.flush()
            continue
        if rid is not None:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                         "result": resp}) + "\n")
            sys.stdout.flush()
