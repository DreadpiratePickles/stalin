"""Run orchestration: fetch → classify → extract → drift → heal → validate → emit.

This is the only module allowed to mutate the lock during a run.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field as dc_field
from typing import Optional

from . import history
from .config import Project, SourceSpec
from .drift import check_source
from .engine import FetchBlocked, RobotsDisallowed, build_engine, classify
from .extract import extract
from .fingerprint import update_stats
from .heal import HealEvent, heal_field
from .lockfile import (Lock, STATUS_BROKEN, STATUS_UNCONFIRMED, STATUS_VERIFIED)
from .llm import LLMUnavailable, OllamaClient
from .schema import json_schema, schema_sha
from .utils import ema, now_iso


@dataclass
class RunResult:
    source: str
    status: str                       # ok | healed | stale | broken | blocked | not-modified
    items: list = dc_field(default_factory=list)
    envelope: dict = dc_field(default_factory=dict)
    heal_events: list[HealEvent] = dc_field(default_factory=list)
    drift_reports: list = dc_field(default_factory=list)
    fetch_elapsed: float = 0.0
    fetch_status: int = 0
    fetch_bytes: int = 0
    error: str = ""
    exit_code: int = 0


def _envelope(project: Project, spec: SourceSpec, items: list, *,
              stale: bool, healed_fields: list[str], fetched_at: str) -> dict:
    sch = json_schema(spec.name, spec.ftypes, spec.contract.version)
    meta: dict = {"schema_version": spec.contract.version,
                  "schema_sha256": schema_sha(sch)}
    if healed_fields:
        meta["healed"] = True
        meta["healed_fields"] = healed_fields
    return {"source": spec.name, "url": spec.url, "fetched_at": fetched_at,
            "schema_version": spec.contract.version, "stale": stale,
            "count": len(items), "items": items, "_meta": meta}


def _load_last_good(project: Project, spec: SourceSpec) -> Optional[dict]:
    p = project.data_path(spec.name)
    if p.exists():
        try:
            env = json.loads(p.read_text())
            env["stale"] = True
            return env
        except json.JSONDecodeError:
            return None
    return None


def _save_snapshot(project: Project, name: str, html: str) -> None:
    d = project.snapshot_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "last-good.html").write_text(html)
    stamped = d / f"{now_iso().replace(':', '.')}.html"
    stamped.write_text(html)
    ring = sorted(p for p in d.glob("2*.html"))
    for old in ring[:-5]:                          # keep last 5
        old.unlink(missing_ok=True)


def _get_llm(project: Project) -> Optional[OllamaClient]:
    try:
        return OllamaClient.create(project.config.llm)
    except LLMUnavailable:
        return None


async def run_source(project: Project, spec: SourceSpec, lock: Lock, *,
                     no_heal: bool = False, force_heal: bool = False,
                     ui=None) -> RunResult:
    """One full run of one source. Saves lock/data/history on success paths."""
    res = RunResult(source=spec.name, status="ok")
    try:
        slock = lock.require(spec.name)
    except KeyError as e:
        res.status = "broken"
        res.error = str(e)
        res.exit_code = 1
        return res

    # --- fetch + classify -------------------------------------------------
    engine = build_engine(project.config, spec)
    from urllib.parse import urlparse
    host = urlparse(spec.url).netloc
    if (spec.robots or project.config.defaults.robots) == "ignore" and ui:
        ui.warn(f"{spec.name}: robots.txt IGNORED for {host} (robots: ignore)")
    try:
        async with engine as eng:
            eng.cache = None                        # per-run engines skip cond. GET for now
            fetch = await eng.fetch(spec.url, conditional=False)
    except RobotsDisallowed as e:
        res.status = "blocked"
        res.error = str(e)
        res.exit_code = 2
        return res
    except FetchBlocked as e:
        res.status = "blocked"
        res.error = e.reason
        res.exit_code = 2
        last = _load_last_good(project, spec)
        if last:
            res.status = "stale"
            res.items = last["items"]
            res.envelope = last
        return res

    res.fetch_elapsed = fetch.elapsed
    res.fetch_status = fetch.status
    res.fetch_bytes = len(fetch.html)
    block_reason = classify(fetch, host, slock.content_len_ema)
    if block_reason:
        res.status = "blocked"
        res.error = block_reason
        res.exit_code = 2
        last = _load_last_good(project, spec)
        if last:
            res.status = "stale"
            res.items = last["items"]
            res.envelope = last
        history.append(project.history_path(spec.name),
                       {"event": "blocked", "reason": block_reason})
        return res

    # --- extract + drift --------------------------------------------------
    base_url = fetch.url_final
    result = extract(fetch.html, slock, spec, base_url)
    reports = check_source(spec, slock, result, project.config.drift)
    if force_heal and not reports:
        from .drift import DriftReport
        reports = [DriftReport(field=n, causes=["forced"]) for n in slock.fields]
    res.drift_reports = reports

    healed_fields: list[str] = []
    if reports:
        if no_heal:
            res.status = "broken"
            res.exit_code = 3
            res.error = "drift detected (healing disabled with --no-heal)"
            last = _load_last_good(project, spec)
            if last:
                res.status = "stale"
                res.items = last["items"]
                res.envelope = last
            for rep in reports:
                history.append(project.history_path(spec.name),
                               {"event": "drift", "field": rep.field,
                                "cause": rep.causes, "healed": False})
            return res

        # --- heal ladder --------------------------------------------------
        old_html = None
        snap = project.snapshot_dir(spec.name) / "last-good.html"
        if snap.exists():
            old_html = snap.read_text()
        client = _get_llm(project)
        for rep in reports:
            fl = slock.fields.get(rep.field)
            if fl is None:
                continue
            was_unconfirmed = fl.status == STATUS_UNCONFIRMED
            was_broken = fl.status == STATUS_BROKEN
            if ui:
                ui.drift_panel_start(spec.name, rep, fl)
            ev = heal_field(rep.field, rep.causes, spec, slock, fetch.html,
                            old_html, base_url, client,
                            on_event=(ui.heal_progress if ui else None),
                            allow_llm=force_heal or not was_broken)
            res.heal_events.append(ev)
            history.append(project.history_path(spec.name), {
                "event": "heal" if ev.outcome == "healed" else "heal-failed",
                "field": ev.field, "cause": ev.causes, "outcome": ev.outcome,
                "old": ev.old, "new": ev.new, "via": ev.via, "model": ev.model,
                "rounds": ev.rounds, "gates": ev.gates,
                "samples_before": ev.samples_before,
                "samples_after": ev.samples_after, "note": ev.note,
                "heal_id": ev.heal_id,
            })
            if ev.outcome == "healed" and was_unconfirmed:
                # drift during the unconfirmed window: heal thrash. Revert hard.
                fl.status = STATUS_BROKEN
                ev.outcome = "broken"
                ev.note = ("drift re-fired while a previous heal was unconfirmed "
                           "(possible A/B testing) — reverted; run "
                           f"`stalin heal {spec.name}.{ev.field} --interactive`")
                history.append(project.history_path(spec.name),
                               {"event": "thrash-revert", "field": ev.field})
            if ev.outcome == "healed":
                healed_fields.append(ev.field)
            if ui:
                ui.heal_panel_end(ev)

        # re-extract with (possibly) healed selectors
        result = extract(fetch.html, slock, spec, base_url)

    # --- status bookkeeping ----------------------------------------------
    broken = [n for n, fl in slock.fields.items() if fl.status == STATUS_BROKEN]
    required_broken = [n for n in broken if not spec.fields[n].ftype.nullable]
    for name, fl in slock.fields.items():
        if fl.status == STATUS_UNCONFIRMED and name not in [e.field for e in res.heal_events]:
            fl.confirm_runs += 1
            if fl.confirm_runs >= 2:
                fl.status = STATUS_VERIFIED
                fl.verified_at = now_iso()
                history.append(project.history_path(spec.name),
                               {"event": "heal-confirmed", "field": name})
        st = result.stats.get(name)
        if st is not None and fl.status != STATUS_BROKEN:
            update_stats(fl.fingerprint, st.match_count, st.null_count,
                         [r for r in st.raws if not isinstance(r, list)])
    slock.content_len_ema = ema(slock.content_len_ema, float(len(fetch.html)))
    slock.status = STATUS_BROKEN if broken else (
        STATUS_UNCONFIRMED if healed_fields else STATUS_VERIFIED)
    lock.save(project.lock_path)

    # --- emit -------------------------------------------------------------
    if required_broken:
        res.status = "broken"
        res.exit_code = 4 if any(
            e.outcome == "absent" for e in res.heal_events) else 3
        res.error = (f"field(s) {', '.join(required_broken)} broken — "
                     "serving last-good")
        last = _load_last_good(project, spec)
        if last:
            res.status = "stale"
            res.items = last["items"]
            res.envelope = last
        return res

    items = [row for row in result.items
             if any(v is not None for v in row.values())]
    env = _envelope(project, spec, items, stale=False,
                    healed_fields=healed_fields, fetched_at=now_iso())
    p = project.data_path(spec.name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(env, ensure_ascii=False, indent=1))
    _save_snapshot(project, spec.name, fetch.html)
    res.items = items
    res.envelope = env
    res.status = "healed" if healed_fields else "ok"
    return res


def run_sources_sync(project: Project, names: list[str], **kw) -> list[RunResult]:
    async def _go():
        lock = Lock.load(project.lock_path)
        out = []
        for name in names:
            spec = project.load_source(name)
            out.append(await run_source(project, spec, lock, **kw))
        return out
    return asyncio.run(_go())
