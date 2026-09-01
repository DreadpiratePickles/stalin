"""Live parameterized lookups — the `live_lookup` mode.

Turns a site's search/detail/profile page into GET /v1/<source>?param=value.
The param binds into a URL template at request time and is scraped LIVE.

Healing is anchored to a heal FIXTURE (a known-good set of params), not to the
per-request page — because different queries legitimately return different
content. A live request only triggers a heal when it comes back empty AND the
page is not a recognized not-found page; the heal then runs against the fixture
(stable, known-good structure) and the fixed selectors are re-applied to the
caller's query. This keeps per-query variation from ever looking like drift.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field as dc_field
from typing import Optional
from urllib.parse import urlparse

from .config import Project, SourceSpec
from .drift import check_source
from .engine import FetchBlocked, RobotsDisallowed, build_engine, classify
from .extract import extract, parse_html
from .fingerprint import update_stats
from .lockfile import Lock, SourceLock, STATUS_BROKEN
from .schema import json_schema, schema_sha
from .utils import now_iso


@dataclass
class LookupResult:
    source: str
    params: dict
    status: str                        # ok | not_found | blocked | broken | error
    items: list = dc_field(default_factory=list)
    envelope: dict = dc_field(default_factory=dict)
    healed_fields: list = dc_field(default_factory=list)
    error: str = ""
    elapsed: float = 0.0
    exit_code: int = 0


_MEMO: dict = {}                       # (source, frozenparams) -> (ts, envelope)


def _page_title(html: str) -> str:
    import re
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return (m.group(1).strip() if m else "")[:200]


def _envelope(spec: SourceSpec, params: dict, items: list, *, status: str,
              healed: list) -> dict:
    sch = json_schema(spec.name, spec.ftypes, spec.contract.version)
    meta = {"schema_version": spec.contract.version,
            "schema_sha256": schema_sha(sch), "status": status}
    if healed:
        meta["healed"] = True
        meta["healed_fields"] = healed
    return {"source": spec.name, "params": params, "url": spec.bind_url(params),
            "fetched_at": now_iso(), "schema_version": spec.contract.version,
            "status": status, "count": len(items), "items": items, "_meta": meta}


async def _fetch(project: Project, spec: SourceSpec, url: str):
    engine = build_engine(project.config, spec)
    async with engine as eng:
        return await eng.fetch(url, conditional=False)


async def run_lookup(project: Project, spec: SourceSpec, lock: Lock,
                     values: dict, *, allow_heal: bool = True) -> LookupResult:
    """Execute one live parameterized lookup."""
    res = LookupResult(source=spec.name, params=values, status="ok")
    missing = spec.missing_params(values)
    if missing:
        res.status = "error"
        res.error = f"missing required param(s): {', '.join(missing)}"
        res.exit_code = 1
        return res
    try:
        slock = lock.require(spec.name)
    except KeyError as e:
        res.status = "broken"
        res.error = str(e)
        res.exit_code = 1
        return res

    # memoization
    if spec.cache_ttl:
        key = (spec.name, tuple(sorted(values.items())))
        hit = _MEMO.get(key)
        if hit and time.time() - hit[0] < spec.cache_ttl:
            env = dict(hit[1])
            env["_meta"] = {**env["_meta"], "cached": True}
            res.envelope = env
            res.items = env["items"]
            res.status = env["status"]
            return res

    url = spec.bind_url(values)
    t0 = time.time()
    try:
        fetch = await _fetch(project, spec, url)
    except RobotsDisallowed as e:
        res.status = "blocked"; res.error = str(e); res.exit_code = 2
        return res
    except FetchBlocked as e:
        res.status = "blocked"; res.error = e.reason; res.exit_code = 2
        return res
    res.elapsed = time.time() - t0

    host = urlparse(url).netloc
    block = classify(fetch, host, slock.content_len_ema)
    if block:
        res.status = "blocked"; res.error = block; res.exit_code = 2
        return res

    title = _page_title(fetch.html)
    if spec.not_found and spec.not_found.matches(fetch.html, fetch.status, title):
        res.status = "not_found"
        res.envelope = _envelope(spec, values, [], status="not_found", healed=[])
        return res

    base = fetch.url_final
    result = extract(fetch.html, slock, spec, base)
    items = [r for r in result.items if any(v is not None for v in r.values())]

    # cheap path: got typed items -> return, no heal (counts vary per query)
    if items:
        for name, fl in slock.fields.items():
            st = result.stats.get(name)
            if st is not None and fl.status != STATUS_BROKEN:
                update_stats(fl.fingerprint, st.match_count, st.null_count,
                             [r for r in st.raws if not isinstance(r, list)])
        res.items = items
        res.envelope = _envelope(spec, values, items, status="ok", healed=[])
        if spec.cache_ttl:
            _MEMO[(spec.name, tuple(sorted(values.items())))] = (time.time(), res.envelope)
        return res

    # empty + not a not-found page: suspicious. Heal against the FIXTURE.
    # (heuristic rung needs no LLM, so we try regardless of Ollama availability)
    if allow_heal and spec.heal_fixture:
        healed = await _heal_against_fixture(project, spec, lock, slock)
        if healed:
            res.healed_fields = healed
            # re-apply healed selectors to the caller's query
            result = extract(fetch.html, slock, spec, base)
            items = [r for r in result.items if any(v is not None for v in r.values())]
            lock.save(project.lock_path)
            if items:
                res.items = items
                res.envelope = _envelope(spec, values, items, status="ok", healed=healed)
                return res

    # genuinely empty (query has no results, or unhealable)
    res.status = "ok" if not slock_broken(slock) else "broken"
    res.envelope = _envelope(spec, values, [], status=res.status, healed=res.healed_fields)
    if slock_broken(slock):
        res.exit_code = 3
    return res


def slock_broken(slock: SourceLock) -> bool:
    return any(fl.status == STATUS_BROKEN for fl in slock.fields.values())


def _get_llm_or_none(project: Project):
    from .llm import LLMUnavailable, OllamaClient
    try:
        return OllamaClient.create(project.config.llm)
    except LLMUnavailable:
        return None


async def _heal_against_fixture(project: Project, spec: SourceSpec, lock: Lock,
                                slock: SourceLock) -> list:
    """Re-fetch the known-good fixture URL, run drift+heal there, return healed fields."""
    from .heal import heal_field
    fixture_url = spec.bind_url(spec.heal_fixture)
    try:
        fetch = await _fetch(project, spec, fixture_url)
    except (FetchBlocked, RobotsDisallowed):
        return []
    host = urlparse(fixture_url).netloc
    if classify(fetch, host, slock.content_len_ema):
        return []                      # fixture itself blocked — never heal
    base = fetch.url_final
    result = extract(fetch.html, slock, spec, base)
    reports = check_source(spec, slock, result, project.config.drift)
    if not reports:
        return []
    client = _get_llm_or_none(project)
    snap = project.snapshot_dir(spec.name) / "last-good.html"
    old_html = snap.read_text() if snap.exists() else None
    healed = []
    for rep in reports:
        ev = heal_field(rep.field, rep.causes, spec, slock, fetch.html, old_html,
                        base, client)
        if ev.outcome == "healed":
            healed.append(ev.field)
    if healed:
        # refresh the fixture snapshot to the new known-good
        d = project.snapshot_dir(spec.name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "last-good.html").write_text(fetch.html)
    return healed
