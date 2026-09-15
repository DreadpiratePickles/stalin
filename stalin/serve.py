"""`stalin serve` — a tiny hand-rolled ASGI app on uvicorn.

Cached snapshots, not live scrape per request: requests return the last-good
run instantly with fetched_at + Age. Freshness comes from --refresh or
POST /v1/{source}/refresh. Live-scrape-per-request is a footgun.
"""
from __future__ import annotations

import asyncio
import json
import time

from .config import Project
from .lockfile import Lock
from .schema import json_schema
from .utils import parse_duration


def _json_response(status: int, body: dict | list, extra_headers: list = ()) -> tuple:
    payload = json.dumps(body, ensure_ascii=False).encode()
    headers = [(b"content-type", b"application/json; charset=utf-8"),
               (b"content-length", str(len(payload)).encode()), *extra_headers]
    return status, headers, payload


def _openapi(project: Project) -> dict:
    paths = {}
    for name in project.source_names():
        try:
            spec = project.load_source(name)
        except Exception:
            continue
        sch = json_schema(name, spec.ftypes, spec.contract.version)
        params_doc = [{"name": pn, "in": "query",
                       "required": pp.required,
                       "schema": {"type": "string"},
                       "description": pp.desc}
                      for pn, pp in spec.params.items()] if spec.is_lookup else []
        # Q-layer: five controls + one equality param per field, correctly typed.
        _t = {"int": "integer", "float": "number", "bool": "boolean"}
        ops_by_field = {}
        for fn, ft in spec.ftypes.items():
            base = ["eq", "ne", "in", "isnull"]
            if ft.base in ("int", "float", "datetime"):
                base += ["gt", "gte", "lt", "lte"]
            if ft.is_list or ft.base in ("str", "url"):
                base += ["contains"]
            ops_by_field[fn] = base
            params_doc.append({
                "name": fn, "in": "query", "required": False,
                "schema": {"type": _t.get(ft.base, "string")},
                "description": f"filter {fn} ({ft.spec}); operators: "
                               + ", ".join(f"{fn}__{o}" for o in base)})
        params_doc += [
            {"name": "sort", "in": "query", "required": False,
             "schema": {"type": "string"},
             "description": "e.g. -" + (next(iter(spec.ftypes), "field"))
                            + " (- = desc, comma = tiebreak)"},
            {"name": "fields", "in": "query", "required": False,
             "schema": {"type": "string"}, "description": "comma list to project"},
            {"name": "limit", "in": "query", "required": False,
             "schema": {"type": "integer", "maximum": 1000}},
            {"name": "offset", "in": "query", "required": False,
             "schema": {"type": "integer", "minimum": 0}},
            {"name": "q", "in": "query", "required": False,
             "schema": {"type": "string"},
             "description": "case-insensitive substring across text fields"},
        ]
        summary = (f"Live lookup {name} by {', '.join(spec.params)}"
                   if spec.is_lookup else f"Latest {name} data (cached snapshot)")
        paths[f"/v1/{name}"] = {
            "get": {
                "summary": summary,
                "x-stalin-operators": ops_by_field,
                "parameters": params_doc,
                "responses": {
                    "400": {"$ref": "#/components/responses/InvalidQuery"},
                    "200": {
                    "description": "envelope",
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string"},
                            "fetched_at": {"type": "string"},
                            "schema_version": {"type": "string"},
                            "stale": {"type": "boolean"},
                            "count": {"type": "integer"},
                            "items": {"type": "array", "items": sch},
                        }}}}}}}}
        paths[f"/v1/{name}/schema"] = {
            "get": {"summary": f"JSON Schema for {name}",
                    "responses": {"200": {"description": "schema"}}}}
    return {"openapi": "3.1.0",
            "info": {"title": "stalin API",
                     "description": "Self-healing, queryable typed JSON from websites.",
                     "version": "0.4.0"},
            "components": {"responses": {"InvalidQuery": {
                "description": "Malformed query (bad filter, operator, or control)",
                "content": {"application/json": {"schema": {"type": "object",
                    "properties": {
                        "error": {"type": "string"}, "param": {"type": "string"},
                        "code": {"type": "string"}, "expected_type": {"type": "string"},
                        "detail": {"type": "string"}}}}}}}},
            "paths": paths}


class RefreshGuard:
    """Rate-limits POST /refresh to one in-flight + 30s cooldown per source."""

    def __init__(self):
        self.last: dict[str, float] = {}
        self.inflight: set[str] = set()

    def allow(self, name: str) -> bool:
        return name not in self.inflight and time.monotonic() - self.last.get(name, 0) > 30


def make_app(project: Project):
    guard = RefreshGuard()

    async def refresh_source(name: str):
        from .runner import run_source
        guard.inflight.add(name)
        try:
            lock = Lock.load(project.lock_path)
            spec = project.load_source(name)
            await run_source(project, spec, lock)
        finally:
            guard.inflight.discard(name)
            guard.last[name] = time.monotonic()

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        path = scope["path"].rstrip("/") or "/"
        method = scope["method"]
        names = project.source_names()

        status, headers, payload = _json_response(404, {"error": "not found",
                                                        "sources": names})
        if path == "/healthz":
            lock = Lock.load(project.lock_path)
            per = {}
            for n in names:
                sl = lock.sources.get(n)
                dp = project.data_path(n)
                age = None
                if dp.exists():
                    try:
                        env = json.loads(dp.read_text())
                        age = env.get("fetched_at")
                    except json.JSONDecodeError:
                        pass
                per[n] = {"status": sl.status if sl else "uncompiled",
                          "last_good": age}
            status, headers, payload = _json_response(200, {"ok": True, "sources": per})
        elif path == "/openapi.json":
            status, headers, payload = _json_response(200, _openapi(project))
        elif path.startswith("/v1/"):
            rest = path[4:]
            if rest.endswith("/schema") and rest[:-7] in names:
                name = rest[:-7]
                spec = project.load_source(name)
                status, headers, payload = _json_response(
                    200, json_schema(name, spec.ftypes, spec.contract.version))
            elif rest.endswith("/refresh") and rest[:-8] in names and method == "POST":
                name = rest[:-8]
                if guard.allow(name):
                    asyncio.get_event_loop().create_task(refresh_source(name))
                    status, headers, payload = _json_response(
                        202, {"refreshing": name, "poll": f"/v1/{name}"})
                else:
                    status, headers, payload = _json_response(
                        429, {"error": "refresh already running or cooling down"})
            elif rest.endswith("/ask") and rest[:-4] in names and method == "GET":
                from urllib.parse import parse_qs
                from .ask import run_ask
                name = rest[:-4]
                spec = project.load_source(name)
                qs = parse_qs(scope.get("query_string", b"").decode())
                question = (qs.get("q") or qs.get("question") or [""])[0]
                if not question:
                    status, headers, payload = _json_response(
                        400, {"error": "missing 'q' (the question)"})
                else:
                    env = await run_ask(project, spec, question)
                    code = 400 if env.get("error") else 200
                    status, headers, payload = _json_response(code, env)
            elif rest in names and method == "GET":
                spec = project.load_source(rest)
                from urllib.parse import parse_qs
                from .query import parse_query, apply, QueryError, query_envelope
                qs = parse_qs(scope.get("query_string", b"").decode())
                raw_params = {k: v[0] for k, v in qs.items()}
                try:
                    query, lookup_args = parse_query(
                        raw_params, spec.ftypes,
                        set(spec.params) if spec.is_lookup else set())
                except QueryError as qe:
                    status, headers, payload = _json_response(400, qe.as_dict())
                else:
                    if spec.is_lookup:
                        missing = spec.missing_params(lookup_args)
                        if missing:
                            status, headers, payload = _json_response(
                                400, {"error": "invalid_query", "code": "missing_param",
                                      "detail": f"missing required param(s): {', '.join(missing)}",
                                      "params": {n: p.type for n, p in spec.params.items()}})
                        else:
                            from .lookup import run_lookup
                            lock = Lock.load(project.lock_path)
                            lres = await run_lookup(project, spec, lock, lookup_args)
                            code = {"ok": 200, "not_found": 200, "blocked": 502,
                                    "broken": 503, "error": 400}.get(lres.status, 200)
                            env = query_envelope(lres.envelope, query, spec.ftypes)
                            status, headers, payload = _json_response(code, env)
                    else:
                        dp = project.data_path(rest)
                        if dp.exists():
                            base = json.loads(dp.read_text())
                            env = query_envelope(base, query, spec.ftypes)
                            status, headers, payload = _json_response(200, env)
                        else:
                            status, headers, payload = _json_response(
                                503, {"error": f"no data yet for {rest!r} — run `stalin run {rest}`"})
        await send({"type": "http.response.start", "status": status,
                    "headers": headers})
        await send({"type": "http.response.body", "body": payload})

    return app


def serve(project: Project, host: str, port: int, refresh: str | None = None):
    import uvicorn
    app = make_app(project)

    if refresh:
        interval = parse_duration(refresh)
        inner = app

        async def app_with_refresh(scope, receive, send):
            if scope["type"] == "lifespan":
                task = None

                async def loop():
                    from .runner import run_source
                    while True:
                        await asyncio.sleep(interval)
                        lock = Lock.load(project.lock_path)
                        for n in project.source_names():
                            try:
                                await run_source(project, project.load_source(n), lock)
                            except Exception:
                                pass

                while True:
                    msg = await receive()
                    if msg["type"] == "lifespan.startup":
                        task = asyncio.get_event_loop().create_task(loop())
                        await send({"type": "lifespan.startup.complete"})
                    elif msg["type"] == "lifespan.shutdown":
                        if task:
                            task.cancel()
                        await send({"type": "lifespan.shutdown.complete"})
                        return
            else:
                await inner(scope, receive, send)

        app = app_with_refresh

    uvicorn.run(app, host=host, port=port, log_level="warning")
