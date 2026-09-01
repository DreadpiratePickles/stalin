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
        summary = (f"Live lookup {name} by {', '.join(spec.params)}"
                   if spec.is_lookup else f"Latest {name} data (cached snapshot)")
        paths[f"/v1/{name}"] = {
            "get": {
                "summary": summary,
                "parameters": params_doc,
                "responses": {"200": {
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
                     "description": "Self-healing typed JSON from websites.",
                     "version": "0.1.0"},
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
            elif rest in names and method == "GET":
                spec = project.load_source(rest)
                if spec.is_lookup:
                    from urllib.parse import parse_qs
                    qs = parse_qs(scope.get("query_string", b"").decode())
                    values = {k: v[0] for k, v in qs.items()}
                    missing = spec.missing_params(values)
                    if missing:
                        status, headers, payload = _json_response(
                            400, {"error": f"missing required param(s): {', '.join(missing)}",
                                  "params": {n: p.type for n, p in spec.params.items()}})
                    else:
                        from .lookup import run_lookup
                        lock = Lock.load(project.lock_path)
                        lres = await run_lookup(project, spec, lock, values)
                        code = {"ok": 200, "not_found": 200, "blocked": 502,
                                "broken": 503, "error": 400}.get(lres.status, 200)
                        status, headers, payload = _json_response(code, lres.envelope)
                else:
                    dp = project.data_path(rest)
                    if dp.exists():
                        env = json.loads(dp.read_text())
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
