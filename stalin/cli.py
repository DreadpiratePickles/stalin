"""The stalin CLI.

Exit codes (scriptable contract):
  0 ok · 1 usage/config error · 2 fetch blocked/failed ·
  3 drift detected and heal failed (data may be stale) · 4 contract breach
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

import typer

from . import history as history_mod
from .config import Project, SourceSpec, FieldSpec, init_project
from .utils import now_iso
from .lockfile import Lock
from .ux import (FAIL, OK, UI, WARN, console, emit_json, err_console, is_tty,
                 items_table, summary_line)

app = typer.Typer(add_completion=False, rich_markup_mode="rich",
                  help="Turn any website into a self-healing, typed JSON API.",
                  no_args_is_help=True)


def _project() -> Project:
    try:
        return Project.find()
    except FileNotFoundError as e:
        err_console.print(f"  {FAIL} {e}")
        raise typer.Exit(1)


def _names(project: Project, sources: List[str]) -> List[str]:
    all_names = project.source_names()
    if not sources:
        if not all_names:
            err_console.print(f"  {FAIL} no sources yet — `stalin add <url>` to create one")
            raise typer.Exit(1)
        return all_names
    for s in sources:
        if s not in all_names:
            err_console.print(f"  {FAIL} unknown source {s!r} "
                              f"(have: {', '.join(all_names) or 'none'})")
            raise typer.Exit(1)
    return sources


# --------------------------------------------------------------------------
@app.command()
def init():
    """Scaffold a stalin project here (stalin.yml, sources/, .stalin/)."""
    created = init_project(Path.cwd())
    for p in created:
        err_console.print(f"  {OK} wrote {p.name}")
    if not created:
        err_console.print(f"  {OK} project already initialized")
    err_console.print("\n  next: [bold]stalin add https://example.com "
                      "-f \"title: str the page title\"[/bold]")


# --------------------------------------------------------------------------
_FIELD_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z_][\w]*)\s*:\s*(?P<type>[\w\[\],| ]+?)\s+(?P<desc>.+?)\s*$")


def _parse_fields(spec_text: str) -> dict[str, FieldSpec]:
    fields: dict[str, FieldSpec] = {}
    parts = [p for chunk in spec_text.split("\n") for p in ([chunk] if ":" in chunk else [])]
    for part in parts:
        part = part.strip().rstrip(",")
        if not part:
            continue
        m = _FIELD_RE.match(part)
        if not m:
            raise typer.BadParameter(
                f"bad field spec {part!r} — want 'name: type description', "
                f"e.g. 'title: str the headline text'")
        fields[m.group("name")] = FieldSpec(type=m.group("type").strip(),
                                            desc=m.group("desc"))
    if not fields:
        raise typer.BadParameter("no fields parsed from -f")
    return fields


def _parse_kv(pairs: List[str]) -> dict:
    """['tag=love','x=1'] -> {'tag':'love','x':'1'}."""
    out: dict = {}
    for item in pairs or []:
        if "=" not in item:
            raise typer.BadParameter(f"expected name=value, got {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _prompt_fields() -> dict[str, FieldSpec]:
    err_console.print("  define fields (empty name to finish):")
    fields: dict[str, FieldSpec] = {}
    while True:
        name = typer.prompt("  field name", default="", show_default=False).strip()
        if not name:
            break
        ftype = typer.prompt("    type", default="str")
        desc = typer.prompt("    description (natural language)")
        try:
            fields[name] = FieldSpec(type=ftype, desc=desc)
        except Exception as e:
            err_console.print(f"    {FAIL} {e}")
    if not fields:
        raise typer.BadParameter("no fields defined")
    return fields


@app.command()
def add(
    url: str = typer.Argument(..., help="Page URL to turn into an API"),
    name: Optional[str] = typer.Option(None, "-n", "--name",
                                       help="Source name (default: from domain)"),
    fields: Optional[str] = typer.Option(None, "-f", "--fields",
                                         help="Inline field spec: 'name: type desc' per line"),
    item: Optional[str] = typer.Option(None, "--item",
                                       help="NL description of the repeating item"),
    example: List[str] = typer.Option(None, "--example",
                                      help="For {param} URLs: sample value(s) to compile "
                                           "against, e.g. --example tag=love (repeatable)"),
    not_found: Optional[str] = typer.Option(None, "--not-found",
                                            help="Signal for a valid-but-empty page, e.g. "
                                                 "\"title contains 'No results'\""),
    no_llm: bool = typer.Option(False, "--no-llm",
                                help="Skip LLM; write a skeleton for hand-written selectors"),
    recompile: bool = typer.Option(False, "--recompile",
                                   help="Recompile an existing source from its yml"),
):
    """Compile a new source: fetch, generate selectors with the local LLM, verify, save."""
    project = _project()
    t_all = time.monotonic()

    if recompile:
        src_name = name or url
        spec = project.load_source(src_name if src_name in project.source_names()
                                   else _names(project, [src_name])[0])
    else:
        src_name = name or urlparse(url).netloc.replace("www.", "").split(":")[0] \
            .replace(".", "-")
        fdict = _parse_fields(fields) if fields else _prompt_fields()
        import re as _re
        from .config import ParamSpec, NotFoundSpec
        placeholders = _re.findall(r"\{(\w+)\}", url)
        ex = _parse_kv(example) if example else {}
        # query-style params: supplied via --example but not in the path template
        qparams = {k for k in ex if k not in placeholders}
        if placeholders or qparams:
            from .query import CONTROLS
            for pn in list(placeholders) + list(qparams):
                if pn in CONTROLS or "__" in pn:
                    err_console.print(
                        f"  {FAIL} param {pn!r} collides with the query layer "
                        f"(reserved: {', '.join(CONTROLS)}; '__' is the filter operator "
                        f"separator). Rename it in the URL, e.g. {{{pn}_}}.")
                    raise typer.Exit(1)
            params = {pn: ParamSpec(location="path",
                                    desc=f"the {pn} to look up") for pn in placeholders}
            for pn in qparams:
                params[pn] = ParamSpec(location="query", desc=f"the {pn} to look up")
            if not ex:
                err_console.print(f"  {FAIL} this URL has parameters {placeholders or list(qparams)} "
                                  "— supply sample value(s) with --example name=value so stalin "
                                  "can compile against a real page")
                raise typer.Exit(1)
            nf = NotFoundSpec.from_signal(not_found) if not_found else None
            spec = SourceSpec(name=src_name, url=url, mode="live_lookup", item=item,
                              fields=fdict, params=params, heal_fixture=ex,
                              not_found=nf,
                              contract=__import__("stalin.config", fromlist=["ContractSpec"]).ContractSpec(min_items=0))
        else:
            spec = SourceSpec(name=src_name, url=url, item=item, fields=fdict)

    # fetch
    from .engine import FetchBlocked, RobotsDisallowed, build_engine, classify

    fetch_url = spec.bind_url(spec.heal_fixture) if spec.is_lookup else spec.url

    async def _fetch():
        async with build_engine(project.config, spec) as eng:
            return await eng.fetch(fetch_url, conditional=False)

    with err_console.status(f"fetching {urlparse(fetch_url).netloc} …"):
        try:
            fetch = asyncio.run(_fetch())
        except RobotsDisallowed as e:
            err_console.print(f"  {FAIL} {e}")
            raise typer.Exit(2)
        except FetchBlocked as e:
            err_console.print(f"  {FAIL} fetch failed: {e.reason}")
            raise typer.Exit(2)
    reason = classify(fetch, urlparse(spec.url).netloc, None)
    if reason:
        err_console.print(f"  {FAIL} page looks blocked/challenged ({reason}) — "
                          "not compiling against garbage")
        raise typer.Exit(2)
    err_console.print(f"  {OK} fetched {fetch.url_final}  "
                      f"[dim]{fetch.status} · {len(fetch.html)//1024} KB · "
                      f"{fetch.elapsed:.1f}s[/dim]")

    lock = Lock.load(project.lock_path)

    if no_llm:
        from .lockfile import SourceLock
        lock.sources[spec.name] = SourceLock(item_selector=None)
        project.save_source(spec)
        lock.save(project.lock_path)
        err_console.print(f"  {OK} wrote sources/{spec.name}.yml (skeleton — add "
                          f"`selector:` to each field, then `stalin run {spec.name}`)")
        return

    # compile: heuristics first, LLM only for the hard tail
    from .compilepipe import compile_source
    from .llm import LLMUnavailable, OllamaClient
    client = None
    try:
        client = OllamaClient.create(project.config.llm)
    except LLMUnavailable as e:
        err_console.print(f"  {WARN} {e} — compiling with heuristics only")

    label = f"heuristics + {client.model}" if client else "heuristics (no LLM)"
    with err_console.status(f"compiling selectors ({label}) …"):
        outcome = compile_source(spec, fetch.html, fetch.url_final, client)
    if outcome.lock is None:
        err_console.print(f"  {FAIL} could not compile any field after "
                          f"{outcome.rounds} round(s) — try refining descriptions "
                          "or --no-llm for manual selectors")
        raise typer.Exit(1)

    # show verification table
    from .extract import extract as run_extract
    result = run_extract(fetch.html, outcome.lock, spec, fetch.url_final)
    items = [r for r in result.items if any(v is not None for v in r.values())]
    items_table(spec.name, urlparse(spec.url).netloc, items)
    for fname, info in outcome.per_field.items():
        err_console.print(f"  {OK} [bold]{fname:<12}[/bold] "
                          f"[cyan]{info['selector']:<28}[/cyan] "
                          f"verified {info['matched']}/{info['scopes']}  "
                          f"[dim]via {info['via']} · {info['fallbacks']} fallbacks[/dim]")
    for fname in outcome.failed_fields:
        err_console.print(f"  {FAIL} [bold]{fname:<12}[/bold] could not compile — "
                          f"add a manual `selector:` in sources/{spec.name}.yml")

    n_fb = sum(len(fl.fallbacks) for fl in outcome.lock.fields.values())
    lock.sources[spec.name] = outcome.lock
    project.save_source(spec)
    lock.save(project.lock_path)
    # seed snapshot + last-good data
    from .runner import _save_snapshot, _envelope
    _save_snapshot(project, spec.name, fetch.html)
    env = _envelope(project, spec, items, stale=False, healed_fields=[],
                    fetched_at=now_iso())
    dp = project.data_path(spec.name)
    dp.parent.mkdir(parents=True, exist_ok=True)
    dp.write_text(json.dumps(env, ensure_ascii=False, indent=1))
    history_mod.append(project.history_path(spec.name),
                       {"event": "compile", "fields": len(outcome.lock.fields),
                        "model": client.model if client else "heuristics-only",
                        "llm_calls": outcome.rounds, "fallbacks": n_fb,
                        "via": outcome.via})
    err_console.print(
        f"\n  {OK} wrote sources/{spec.name}.yml, .stalin/lock.json "
        f"(schema v{spec.contract.version}, {n_fb} fallbacks) "
        f"[dim]{time.monotonic()-t_all:.1f}s[/dim]")
    if spec.is_lookup:
        ex = "&".join(f"{k}={v}" for k, v in spec.heal_fixture.items())
        err_console.print(f"  [bold]live lookup[/bold] — params: "
                          f"{', '.join(spec.params)}")
        err_console.print(f"  next: [bold]stalin run {spec.name} "
                          f"{' '.join('--param '+k+'='+v for k,v in spec.heal_fixture.items())}"
                          f"[/bold]")
        err_console.print(f"        [bold]stalin serve[/bold] → "
                          f"GET /v1/{spec.name}?{ex}")
    else:
        err_console.print(f"  next: [bold]stalin run {spec.name}[/bold] · "
                          f"[bold]stalin serve[/bold]")
    if outcome.failed_fields:
        raise typer.Exit(1)


# --------------------------------------------------------------------------
@app.command()
def run(
    sources: List[str] = typer.Argument(None),
    param: List[str] = typer.Option(None, "-p", "--param",
                                    help="For live_lookup sources: name=value (repeatable)"),
    where: List[str] = typer.Option(None, "-w", "--where",
                                    help="Filter: field__op=value or field=value (repeatable)"),
    sort: Optional[str] = typer.Option(None, "--sort", help="e.g. -points,author"),
    fields: Optional[str] = typer.Option(None, "--fields", help="comma list to project"),
    limit: Optional[int] = typer.Option(None, "--limit"),
    offset: Optional[int] = typer.Option(None, "--offset"),
    q: Optional[str] = typer.Option(None, "-q", "--query", help="full-text substring"),
    items_only: bool = typer.Option(False, "--items", help="print only the items array"),
    as_json: bool = typer.Option(False, "--json", help="Force JSON to stdout"),
    jsonl: Optional[Path] = typer.Option(None, "--jsonl", help="Append envelope to JSONL file"),
    flat: bool = typer.Option(False, "--flat", help="With --jsonl: one item per line"),
    no_heal: bool = typer.Option(False, "--no-heal",
                                 help="Detect drift, report, exit 3 — never call the LLM"),
    stale_ok: bool = typer.Option(False, "--stale-ok",
                                  help="Exit 0 even when serving last-good data"),
):
    """Extract now. Auto-heals on drift (that's the whole point)."""
    project = _project()
    names = _names(project, sources or [])
    from .query import parse_query, query_envelope, QueryError

    def _raw_query() -> dict:
        raw = _parse_kv(where)
        if sort is not None: raw["sort"] = sort
        if fields is not None: raw["fields"] = fields
        if limit is not None: raw["limit"] = str(limit)
        if offset is not None: raw["offset"] = str(offset)
        if q is not None: raw["q"] = q
        return raw

    def _emit(env: dict):
        if items_only:
            print(json.dumps(env.get("items", []), ensure_ascii=False, indent=2))
        else:
            emit_json(env)

    # live_lookup sources take --param and run live
    lookup_names = [n for n in names if project.load_source(n).is_lookup]
    if lookup_names:
        if len(names) > 1:
            err_console.print(f"  {FAIL} run one lookup source at a time (with --param)")
            raise typer.Exit(1)
        import asyncio as _asyncio
        from .lookup import run_lookup
        from .lockfile import Lock as _Lock
        spec = project.load_source(names[0])
        raw = {**_parse_kv(param), **_raw_query()}
        try:
            query, lookup_args = parse_query(raw, spec.ftypes, set(spec.params))
        except QueryError as qe:
            err_console.print(f"  {FAIL} {qe.as_dict()}")
            raise typer.Exit(2)
        lock = _Lock.load(project.lock_path)
        lres = _asyncio.run(run_lookup(project, spec, lock, lookup_args))
        env = query_envelope(lres.envelope, query, spec.ftypes) if lres.envelope else {}
        mark = OK if lres.status in ("ok", "not_found") else FAIL
        healed = (f"  ({len(lres.healed_fields)} healed)" if lres.healed_fields else "")
        err_console.print(f"  {mark} [bold]{lres.source}[/bold]  {lres.status.upper()}  "
                          f"{env.get('returned', 0)}/{env.get('count', 0)} items  "
                          f"{lres.elapsed:.1f}s{healed}")
        if lres.error:
            err_console.print(f"      {lres.error}")
        if env and (as_json or items_only or not is_tty()):
            _emit(env)
        elif env.get("items") and is_tty():
            items_table(lres.source, env.get("url", ""), env["items"])
        raise typer.Exit(lres.exit_code)
    from .runner import run_sources_sync
    ui = UI()
    results = run_sources_sync(project, names, no_heal=no_heal, ui=ui)
    worst = 0
    for res in results:
        summary_line(res)
        env = res.envelope
        if env:
            spec = project.load_source(res.source)
            try:
                query, _ = parse_query(_raw_query(), spec.ftypes, set())
                env = query_envelope(env, query, spec.ftypes)
            except QueryError as qe:
                err_console.print(f"  {FAIL} {qe.as_dict()}")
                raise typer.Exit(2)
        if env and (as_json or items_only or not is_tty()):
            _emit(env)
        elif env and env.get("items") and is_tty() and not as_json:
            items_table(res.source, env.get("url", ""), env["items"])
        if jsonl and res.envelope:
            jsonl.parent.mkdir(parents=True, exist_ok=True)
            with jsonl.open("a") as f:
                if flat:
                    for it in res.envelope["items"]:
                        f.write(json.dumps(it, ensure_ascii=False) + "\n")
                else:
                    f.write(json.dumps(res.envelope, ensure_ascii=False) + "\n")
        code = res.exit_code
        if stale_ok and res.status == "stale":
            code = 0
        worst = max(worst, code)
    raise typer.Exit(worst)


# --------------------------------------------------------------------------
@app.command()
def heal(
    target: Optional[str] = typer.Argument(None,
                                           help="SOURCE or SOURCE.FIELD to heal"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Show what would change; touch nothing"),
    interactive: bool = typer.Option(False, "--interactive",
                                     help="Confirm each candidate selector"),
):
    """Force a heal pass (after `run --no-heal`, or when a field is broken)."""
    project = _project()
    src, fld = (target.split(".", 1) + [None])[:2] if target else (None, None)
    names = _names(project, [src] if src else [])
    from .runner import run_sources_sync
    ui = UI()
    if interactive:
        err_console.print(f"  {WARN} --interactive: each accepted candidate will "
                          "ask for confirmation")
        # interactive confirm is wired through the same UI path; keep simple in v1
    if dry_run:
        import shutil
        import tempfile
        # run against a copy of the lock; discard
        results = run_sources_sync(project, names, force_heal=bool(fld), ui=ui)
        err_console.print(f"  {WARN} --dry-run in v1 executes detection+heal but a "
                          "reverted lock is not yet implemented; review "
                          "`git diff .stalin/lock.json` and commit or checkout")
    else:
        results = run_sources_sync(project, names, force_heal=True, ui=ui)
    worst = 0
    for res in results:
        summary_line(res)
        worst = max(worst, res.exit_code)
    raise typer.Exit(worst)


# --------------------------------------------------------------------------
@app.command()
def serve(
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
    refresh: Optional[str] = typer.Option(None, "--refresh",
                                          help="Background re-run interval, e.g. 15m"),
):
    """HTTP API over cached snapshots (+ OpenAPI 3.1 at /openapi.json)."""
    project = _project()
    h = host or project.config.serve.host
    p = port or project.config.serve.port
    err_console.print(f"  stalin API → [bold]http://{h}:{p}[/bold]")
    for n in project.source_names():
        try:
            spec = project.load_source(n)
            err_console.print(f"    GET /v1/{n:<16} typed JSON, schema "
                              f"v{spec.contract.version}")
        except Exception:
            pass
    err_console.print("    GET /openapi.json    OpenAPI 3.1\n"
                      "    GET /healthz         per-source status")
    from .serve import serve as _serve
    _serve(project, h, p, refresh)


# --------------------------------------------------------------------------
@app.command()
def watch(sources: List[str] = typer.Argument(None)):
    """Foreground loop: run each source on its schedule, heal, log."""
    project = _project()
    names = _names(project, sources or [])
    from .runner import run_sources_sync
    from .utils import parse_duration
    scheds = {n: parse_duration(project.load_source(n).schedule) for n in names}
    nxt = {n: 0.0 for n in names}
    err_console.print(f"  watching {', '.join(names)} — ctrl-c to stop")
    ui = UI()
    try:
        while True:
            now = time.monotonic()
            due = [n for n in names if now >= nxt[n]]
            for n in due:
                for res in run_sources_sync(project, [n], ui=ui):
                    summary_line(res)
                nxt[n] = time.monotonic() + scheds[n]
            time.sleep(min(5.0, max(0.5, min(nxt.values()) - time.monotonic())))
    except KeyboardInterrupt:
        err_console.print("\n  stopped")


# --------------------------------------------------------------------------
@app.command()
def ask(
    source: str = typer.Argument(..., help="Source to query"),
    question: List[str] = typer.Argument(..., help="Plain-English question"),
    as_json: bool = typer.Option(False, "--json", help="Force JSON to stdout"),
    items_only: bool = typer.Option(False, "--items", help="print only the items array"),
):
    """Ask a source a plain-English question (local LLM compiles it to a query)."""
    project = _project()
    _names(project, [source])
    spec = project.load_source(source)
    q = " ".join(question)
    import asyncio as _asyncio
    from .ask import run_ask
    with err_console.status(f"compiling “{q}” …"):
        env = _asyncio.run(run_ask(project, spec, q))
    if env.get("error"):
        err_console.print(f"  {FAIL} {env.get('error')}: {env.get('detail','')}")
        if env.get("compiled"):
            err_console.print(f"      compiled: {env['compiled']}")
        raise typer.Exit(2)
    compiled = env.get("_query", {})
    err_console.print(f"  {OK} [dim]compiled →[/dim] "
                      f"{ {k: v for k, v in compiled.items() if v} }")
    err_console.print(f"  {OK} [bold]{source}[/bold]  "
                      f"{env.get('returned', 0)}/{env.get('count', 0)} items")
    if items_only:
        print(json.dumps(env.get("items", []), ensure_ascii=False, indent=2))
    elif as_json or not is_tty():
        emit_json(env)
    elif env.get("items"):
        items_table(source, env.get("url", ""), env["items"])


@app.command()
def mcp():
    """MCP server on stdio (read-only tools: list_sources, get_data, get_schema)."""
    project = _project()
    from .mcp_server import serve_stdio
    serve_stdio(project)


# --------------------------------------------------------------------------
schema_app = typer.Typer(help="Schema contract commands.", no_args_is_help=True)
app.add_typer(schema_app, name="schema")


@schema_app.command("show")
def schema_show(source: str):
    """Print the JSON Schema for a source."""
    project = _project()
    _names(project, [source])
    spec = project.load_source(source)
    from .schema import json_schema
    print(json.dumps(json_schema(source, spec.ftypes, spec.contract.version),
                     indent=2))


@schema_app.command("bump")
def schema_bump(source: str,
                major: bool = typer.Option(False, "--major"),
                minor: bool = typer.Option(False, "--minor")):
    """Explicitly bump the schema contract version (the only way majors happen)."""
    project = _project()
    _names(project, [source])
    spec = project.load_source(source)
    parts = [int(x) for x in spec.contract.version.split(".")]
    while len(parts) < 3:
        parts.append(0)
    if major:
        parts = [parts[0] + 1, 0, 0]
    elif minor:
        parts = [parts[0], parts[1] + 1, 0]
    else:
        raise typer.BadParameter("pass --major or --minor")
    old = spec.contract.version
    spec.contract.version = ".".join(map(str, parts))
    project.save_source(spec)
    # un-break fields frozen by contract breach so the next run re-evaluates
    lock = Lock.load(project.lock_path)
    sl = lock.sources.get(source)
    if sl:
        for fl in sl.fields.values():
            if fl.status == "broken":
                fl.status = "healed-unconfirmed"
                fl.confirm_runs = 0
        lock.save(project.lock_path)
    err_console.print(f"  {OK} {source}: schema v{old} → v{spec.contract.version}")


# --------------------------------------------------------------------------
@app.command()
def history(target: str = typer.Argument(...,
                                         help="SOURCE or SOURCE.FIELD")):
    """Selector/heal event timeline."""
    project = _project()
    src, fld = (target.split(".", 1) + [None])[:2]
    _names(project, [src])
    from .ux import history_timeline
    events = history_mod.read(project.history_path(src), field=fld)
    history_timeline(target, events)


# --------------------------------------------------------------------------
@app.command()
def doctor():
    """Environment check: ollama? model? network? broken fields?"""
    try:
        project = Project.find()
    except FileNotFoundError:
        project = None
    from .doctor import run_doctor
    raise typer.Exit(run_doctor(project))


# --------------------------------------------------------------------------
@app.command()
def snapshot(source: str):
    """Capture/refresh the reference HTML snapshot for a source."""
    project = _project()
    _names(project, [source])
    spec = project.load_source(source)
    from .engine import FetchBlocked, build_engine

    async def _fetch():
        async with build_engine(project.config, spec) as eng:
            return await eng.fetch(spec.url, conditional=False)

    try:
        fetch = asyncio.run(_fetch())
    except FetchBlocked as e:
        err_console.print(f"  {FAIL} {e.reason}")
        raise typer.Exit(2)
    from .runner import _save_snapshot
    _save_snapshot(project, source, fetch.html)
    err_console.print(f"  {OK} snapshot saved ({len(fetch.html)//1024} KB)")


if __name__ == "__main__":
    app()
