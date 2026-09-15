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


_QUERY_PROPS = {
    "filter": {"type": "object",
               "description": "Field filters, same spelling as HTTP: keys are "
                              "`field` (equality) or `field__op` where op is one of "
                              "eq/ne/gt/gte/lt/lte/in/contains/isnull. Values may be "
                              "native JSON or strings. e.g. {\"points__gt\": 100}."},
    "sort": {"type": "string", "description": "e.g. '-points,author' (- = desc)"},
    "fields": {"type": "string", "description": "comma list to project, e.g. 'author,text'"},
    "limit": {"type": "integer"},
    "offset": {"type": "integer"},
    "q": {"type": "string", "description": "case-insensitive substring across text fields"},
}


def _field_doc(spec) -> str:
    return "; ".join(f"{n}:{ft.spec}" for n, ft in spec.ftypes.items())


def _query_args_to_params(args: dict) -> dict:
    """Flatten MCP query args into the flat param map parse_query expects."""
    raw = {}
    flt = args.get("filter") or {}
    if isinstance(flt, dict):
        for k, v in flt.items():
            raw[k] = v
    for ctrl in ("sort", "fields", "q"):
        if args.get(ctrl) not in (None, ""):
            raw[ctrl] = str(args[ctrl])
    for ctrl in ("limit", "offset"):
        if args.get(ctrl) is not None:
            raw[ctrl] = str(args[ctrl])
    return raw



TOOLS = [
    {"name": "list_sources",
     "description": "List all configured stalin sources with status and schema version.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_data",
     "description": ("Get the latest extracted data for a source as typed JSON "
                     "(cached last-good snapshot). Supports filter/sort/fields/"
                     "limit/offset/q — see get_schema for field names and types."),
     "inputSchema": {"type": "object",
                     "properties": {"source": {"type": "string"}, **_QUERY_PROPS},
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
        props.update(_QUERY_PROPS)
        required = [pn for pn, pp in spec.params.items() if pp.required]
        tools.append({
            "name": f"lookup_{n}",
            "description": (f"Live lookup of {n} by {', '.join(spec.params)}, scraped "
                            f"on demand from {spec.url}. Returns typed, self-healing "
                            f"JSON. Filterable fields — {_field_doc(spec)}. Use "
                            f"filter/sort/fields/limit/offset/q to narrow results."),
            "inputSchema": {"type": "object", "properties": props, "required": required},
        })
    return tools


def _ask_tools(project: Project) -> list:
    """Every source gets ask_<source>(question) — plain-English querying."""
    tools = []
    for n in project.source_names():
        try:
            spec = project.load_source(n)
        except Exception:
            continue
        tools.append({
            "name": f"ask_{n}",
            "description": (f"Ask {n} a plain-English question; a local LLM "
                            f"compiles it into a typed query (filter/sort/limit/…) "
                            f"over the fields [{_field_doc(spec)}] and returns the "
                            f"matching JSON. The response echoes the compiled query "
                            f"in _query so you can verify it."),
            "inputSchema": {"type": "object",
                            "properties": {"question": {"type": "string"}},
                            "required": ["question"]},
        })
    return tools


def _call_tool(project: Project, name: str, args: dict) -> dict:
    if name.startswith("ask_"):
        src = name[len("ask_"):]
        if src not in project.source_names():
            return {"content": [{"type": "text", "text": f"unknown source {src!r}"}],
                    "isError": True}
        import asyncio
        from .ask import run_ask
        spec = project.load_source(src)
        env = asyncio.run(run_ask(project, spec, (args or {}).get("question", "")))
        if env.get("error"):
            return {"content": [{"type": "text", "text": json.dumps(env)}],
                    "isError": True}
        return _tool_result(env)
    if name.startswith("lookup_"):
        src = name[len("lookup_"):]
        if src not in project.source_names():
            return {"content": [{"type": "text", "text": f"unknown source {src!r}"}],
                    "isError": True}
        import asyncio
        from .lookup import run_lookup
        from .lockfile import Lock
        from .query import parse_query, query_envelope, QueryError
        spec = project.load_source(src)
        args = args or {}
        raw = _query_args_to_params(args)
        for pn in spec.params:                       # declared params from top-level args
            if pn in args and pn not in raw:
                raw[pn] = args[pn]
        try:
            query, lookup_args = parse_query(raw, spec.ftypes, set(spec.params))
        except QueryError as qe:
            return {"content": [{"type": "text",
                                 "text": json.dumps(qe.as_dict())}], "isError": True}
        lock = Lock.load(project.lock_path)
        lres = asyncio.run(run_lookup(project, spec, lock, lookup_args))
        if not lres.envelope:
            return {"content": [{"type": "text",
                                 "text": json.dumps({"error": lres.error, "status": lres.status})}],
                    "isError": True}
        return _tool_result(query_envelope(lres.envelope, query, spec.ftypes))
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
        from .query import parse_query, query_envelope, QueryError
        spec = project.load_source(src)
        try:
            query, _ = parse_query(_query_args_to_params(args), spec.ftypes, set())
        except QueryError as qe:
            return {"content": [{"type": "text",
                                 "text": json.dumps(qe.as_dict())}], "isError": True}
        return _tool_result(query_envelope(json.loads(dp.read_text()), query, spec.ftypes))
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
            resp = {"tools": TOOLS + _lookup_tools(project) + _ask_tools(project)}
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
