"""The heal ladder. The crown jewel.

    drift detected on field F
      ├─ Rung 1: stored fallbacks          (no LLM, ~ms)
      ├─ Rung 2: heuristic re-location     (no LLM, ~ms — DOM statistics scored
      │          against the field fingerprint, judged by the gates)
      ├─ Rung 3: LLM re-location           (sentinel text protocol, judged by
      │          the same gates — works with any local model)
      └─ Rung 4: fail → broken, serve last-good, exit 3

Every accepted heal is applied immediately (data flows — that's the promise)
but marked healed-unconfirmed until two clean runs promote it. The proposers
differ per rung; the judge never changes: deterministic acceptance gates.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Callable, Optional

from .config import FieldSpec, SourceSpec
from .extract import parse_html
from .gates import GateResult, fingerprint_after_accept, run_gates
from .heuristics import field_candidates
from .lockfile import (FieldLock, SelectorDef, SourceLock, STATUS_BROKEN,
                       promote_heal)
from .llm import (LLMUnavailable, OllamaClient, candidate_regions, complete,
                  parse_selector_reply, prune_html)
from .utils import new_id

HEAL_SYSTEM = """You are a web-scraping repair engineer. A field's CSS selector \
broke because the website changed its layout. Find where the SAME data lives \
in the NEW page. Reply with brief reasoning followed by EXACTLY two final lines:

SELECTOR: <css selector relative to one item>
ATTR: <text or an attribute name like href>

Example of a complete correct reply:

The score moved from span.score to a span with class "karma".
SELECTOR: span.karma
ATTR: text

If — and only if — the data is genuinely gone from the page, reply instead \
with one line:  ABSENT: <why>"""


@dataclass
class HealEvent:
    field: str
    causes: list[str]
    outcome: str                      # healed | broken | absent
    via: str = ""                     # fallback | heuristic | llm
    old: str = ""
    new: str = ""
    rounds: int = 0
    gates: dict = dc_field(default_factory=dict)
    samples_before: list = dc_field(default_factory=list)
    samples_after: list = dc_field(default_factory=list)
    heal_id: str = ""
    model: str = ""
    note: str = ""
    attempts: list = dc_field(default_factory=list)


def _accept(fl: FieldLock, sel: SelectorDef, gate: GateResult, ev: HealEvent) -> None:
    ev.outcome = "healed"
    ev.new = sel.pretty()
    ev.gates = gate.summary()
    ev.samples_after = [v for v in gate.probe["values"][:3] if v is not None]
    ev.heal_id = new_id("heal")
    promote_heal(fl, sel)
    fl.fingerprint = fingerprint_after_accept(gate.probe)


def _verify_absent(root, fl: FieldLock) -> bool:
    """Independently verify an ABSENT claim: no old sample values on the page
    and no page-wide value-shape matches. The model asserts; we check."""
    page_text = " ".join(root.itertext())
    for s in fl.fingerprint.samples:
        if s and s in page_text:
            return False
    shape = fl.fingerprint.value_shape
    if shape:
        import re
        try:
            rx = re.compile(shape)
        except re.error:
            return True
        hits = sum(1 for chunk in page_text.split("  ")
                   if rx.match(chunk.strip()[:120]))
        if hits > max(2, 0.3 * (fl.fingerprint.match_count_ema or 0)):
            return False
    return True


def heal_field(name: str, causes: list[str], spec: SourceSpec, lock: SourceLock,
               new_html: str, old_html: Optional[str], base_url: str,
               client: Optional[OllamaClient],
               on_event: Optional[Callable[[str, dict], None]] = None,
               allow_llm: bool = True) -> HealEvent:
    """Run the ladder for one drifted field. Mutates lock on success."""
    fspec: FieldSpec = spec.fields[name]
    fl: FieldLock = lock.fields[name]
    # disjointness: a heal may not land on another field's (selector, attr) —
    # that is how "comments" silently becomes a copy of "points"
    occupied = {(ofl.selector.css, ofl.selector.attr)
                for oname, ofl in lock.fields.items() if oname != name}
    ev = HealEvent(field=name, causes=causes, outcome="broken",
                   old=fl.selector.pretty(),
                   samples_before=list(fl.fingerprint.samples))
    root = parse_html(new_html)
    notify = on_event or (lambda *_: None)

    if fspec.pin:
        ev.note = "field is pinned (pin: true) — healing disabled"
        fl.status = STATUS_BROKEN
        return ev

    def gated(sel: SelectorDef) -> GateResult:
        if (sel.css, sel.attr) in occupied:
            return GateResult(passed=False, detail={"disjoint": False},
                              probe={"matched_scopes": 0, "scopes": 0,
                                     "values": [], "raws": [], "nodes": [],
                                     "cast_ok": 0, "cast_fail": 0})
        return run_gates(root, lock.item_selector, sel, fspec, fl.fingerprint,
                         base_url, spec.contract.min_items)

    # ---- Rung 1: stored fallbacks (no LLM) ------------------------------
    for fb in list(fl.fallbacks):
        gate = gated(fb)
        ev.attempts.append({"rung": "fallback", "css": fb.pretty(),
                            "passed": gate.passed, "detail": gate.summary()})
        notify("fallback", {"css": fb.pretty(), "passed": gate.passed,
                            "detail": gate.summary()})
        if gate.passed:
            fl.fallbacks.remove(fb)
            _accept(fl, fb, gate, ev)
            ev.via = "fallback"
            return ev

    # ---- Rung 2: heuristic re-location (no LLM) -------------------------
    tried = {fl.selector.css} | {a["css"].split(" @")[0] for a in ev.attempts}
    for cand in field_candidates(root, lock.item_selector, fspec.ftype,
                                 fspec.desc, name, base_url,
                                 fp=fl.fingerprint, max_cands=6):
        if cand.css in tried:
            continue
        tried.add(cand.css)
        gate = gated(cand)
        ev.attempts.append({"rung": "heuristic", "css": cand.pretty(),
                            "passed": gate.passed, "detail": gate.summary()})
        notify("candidate", {"css": cand.pretty(), "passed": gate.passed,
                             "detail": gate.summary(),
                             "matched": gate.probe["matched_scopes"],
                             "scopes": gate.probe["scopes"]})
        if gate.passed:
            _accept(fl, cand, gate, ev)
            ev.via = "heuristic"
            return ev

    # ---- Rung 3: LLM re-location (sentinel protocol, any model) ---------
    if not allow_llm:
        ev.note = ("fallbacks and heuristics exhausted; LLM rung reserved for an "
                   "explicit `stalin heal` (field was already broken)")
        fl.status = STATUS_BROKEN
        return ev
    if client is None:
        ev.note = "ollama unavailable — detection-only mode (LLM rung skipped)"
        fl.status = STATUS_BROKEN
        return ev

    old_ctx = ""
    if old_html:
        old_root = parse_html(old_html)
        try:
            scope = (old_root.cssselect(lock.item_selector)[0]
                     if lock.item_selector and old_root.cssselect(lock.item_selector)
                     else old_root)
            old_nodes = scope.cssselect(fl.selector.css)
        except Exception:
            old_nodes = []
        if old_nodes:
            node = old_nodes[0]
            for _ in range(2):
                if node.getparent() is not None:
                    node = node.getparent()
            import lxml.html as LH
            old_ctx = prune_html(LH.tostring(node, encoding="unicode"), 1500)

    regions = candidate_regions(new_html, fl.fingerprint.samples,
                                fl.fingerprint.value_shape, cap=7000)
    base_user = (
        f"Field: {name}\nMeaning: {fspec.desc or name}\nType: {fspec.type}\n"
        f"Old selector (now broken): {fl.selector.css} (attr={fl.selector.attr})\n"
        f"Drift signals: {', '.join(causes)}\n"
        f"Last known-good sample values: {fl.fingerprint.samples}\n"
        f"Item scope selector: {lock.item_selector or '(whole page)'}\n\n"
        + (f"OLD page context around the field:\n```html\n{old_ctx}\n```\n\n"
           if old_ctx else "")
        + f"NEW page candidate regions:\n```html\n{regions}\n```")

    feedback = ""
    for attempt in range(1, 4):
        ev.rounds = attempt
        ev.model = client.model
        try:
            reply = complete(client, HEAL_SYSTEM, base_user + feedback)
        except LLMUnavailable as e:
            ev.note = str(e)
            fl.status = STATUS_BROKEN
            return ev
        parsed = parse_selector_reply(reply)
        if "absent" in parsed:
            if _verify_absent(root, fl):
                ev.outcome = "absent"
                ev.note = parsed["absent"]
                fl.status = STATUS_BROKEN
                return ev
            feedback = ("\n\nYou claimed ABSENT but old sample values or matching "
                        "value shapes still exist on the page — the data is NOT "
                        "gone. Propose a SELECTOR instead.")
            continue
        css = parsed.get("css")
        if not css:
            feedback = ("\n\nYour reply had no SELECTOR: line. End with the two "
                        "required lines (SELECTOR: / ATTR:).")
            continue
        if css in tried:
            feedback = (f"\n\nYou already proposed `{css}` and it failed. "
                        "Propose a DIFFERENT selector.")
            continue
        tried.add(css)
        sel = SelectorDef(css=css, attr=parsed.get("attr", fl.selector.attr))
        gate = gated(sel)
        ev.attempts.append({"rung": "llm", "css": sel.pretty(),
                            "passed": gate.passed, "detail": gate.summary()})
        notify("candidate", {"css": sel.pretty(), "passed": gate.passed,
                             "detail": gate.summary(),
                             "matched": gate.probe["matched_scopes"],
                             "scopes": gate.probe["scopes"]})
        if gate.passed:
            _accept(fl, sel, gate, ev)
            ev.via = "llm"
            return ev
        d = gate.summary()
        feedback = (f"\n\nYou proposed `{css}` (ATTR: {sel.attr}). Result: matched "
                    f"{gate.probe['matched_scopes']}/{gate.probe['scopes']} items, "
                    f"gates: {d}. Sample values it extracted: "
                    f"{[v for v in gate.probe['values'][:3] if v is not None]}. "
                    "Fix what failed and reply again with SELECTOR:/ATTR:.")

    # ---- Rung 4: exhausted ----------------------------------------------
    fl.status = STATUS_BROKEN
    ev.note = "heal exhausted (fallbacks, heuristics, 3 LLM attempts) — serving last-good"
    return ev
