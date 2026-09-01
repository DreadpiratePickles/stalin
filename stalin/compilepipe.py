"""`stalin add` — compile natural-language field descriptions into selectors.

Council architecture (h/t karpathy/llm-council): many cheap proposers, one
deterministic judge. The heuristic engine proposes candidates from DOM
statistics in milliseconds; the acceptance criteria judge them; the local LLM
is consulted per-field, one job per call, only for what heuristics can't
cover — and it answers in a sentinel-marked text protocol that any model can
speak, not fragile structured tool calls.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Callable, Optional

from .config import SourceSpec
from .extract import parse_html, probe_selector
from .fingerprint import fingerprint_from_nodes
from .heuristics import field_candidates, item_selector_candidates
from .lockfile import FieldLock, SelectorDef, SourceLock, STATUS_VERIFIED
from .llm import (LLMUnavailable, OllamaClient, complete, parse_selector_reply,
                  prune_html)
from .utils import now_iso


@dataclass
class CompileOutcome:
    lock: Optional[SourceLock]
    per_field: dict[str, dict] = dc_field(default_factory=dict)
    rounds: int = 0                                  # LLM calls made
    failed_fields: list[str] = dc_field(default_factory=list)
    via: dict[str, str] = dc_field(default_factory=dict)   # field -> heuristic|llm|manual


def _passes(pr: dict, nullable: bool) -> bool:
    n = max(pr["scopes"], 1)
    return (pr["matched_scopes"] > 0 and pr["cast_fail"] == 0
            and pr["matched_scopes"] / n >= (0.4 if nullable else 0.75))


def _pick_item_selector(spec: SourceSpec, root, base_url: str) -> Optional[str]:
    """Choose the item selector that lets the most fields find a passing
    heuristic candidate. Falls back to None (single-record page)."""
    cands = item_selector_candidates(root, spec.contract.min_items)
    if spec.item is None and not cands:
        return None
    best, best_score = None, -1
    options = cands[:5] + ([None] if spec.item is None else [])
    for item_sel in options:
        score = 0
        for name, fspec in spec.fields.items():
            fc = field_candidates(root, item_sel, fspec.ftype, fspec.desc,
                                  name, base_url, max_cands=3)
            for sel in fc:
                pr = probe_selector(root, item_sel, sel, fspec.ftype, base_url)
                if _passes(pr, fspec.ftype.nullable):
                    score += 1
                    break
        n_scopes = len(root.cssselect(item_sel)) if item_sel else 1
        if score > best_score or (score == best_score and best is None):
            best, best_score = item_sel, score
    return best


LLM_SYSTEM = """You are a CSS selector engineer. Given HTML and ONE field to \
extract, reply with your reasoning followed by EXACTLY two final lines:

SELECTOR: <css selector relative to one item>
ATTR: <text or an attribute name like href>

Example of a complete correct reply:

The price lives in a span with class "amount" inside each card.
SELECTOR: span.amount
ATTR: text"""


def llm_field_selector(client: OllamaClient, root, item_selector: Optional[str],
                       spec: SourceSpec, name: str, base_url: str,
                       snippets: str, max_attempts: int = 2,
                       counter: Optional[list] = None) -> Optional[SelectorDef]:
    """One field, one job per call, sentinel protocol, probe-verified."""
    fspec = spec.fields[name]
    user = (f"Field to extract: {name}\nType: {fspec.type}\n"
            f"Meaning: {fspec.desc or name}\n"
            f"Item scope selector: {item_selector or '(whole page)'}\n\n"
            f"HTML of two items:\n```html\n{snippets}\n```")
    feedback = ""
    for _ in range(max_attempts):
        if counter is not None:
            counter.append(1)
        try:
            reply = complete(client, LLM_SYSTEM, user + feedback)
        except LLMUnavailable:
            return None
        parsed = parse_selector_reply(reply)
        css = parsed.get("css")
        if not css:
            feedback = "\n\nYour last reply had no SELECTOR: line. End with the two required lines."
            continue
        sel = SelectorDef(css=css, attr=parsed.get("attr", "text"))
        pr = probe_selector(root, item_selector, sel, fspec.ftype, base_url)
        if _passes(pr, fspec.ftype.nullable):
            return sel
        n = max(pr["scopes"], 1)
        feedback = (f"\n\nYou proposed `{css}` (ATTR: {sel.attr}) but it matched "
                    f"{pr['matched_scopes']}/{n} items with {pr['cast_fail']} "
                    f"type-cast failures. Sample values: "
                    f"{[v for v in pr['values'][:3] if v is not None]}. Try again.")
    return None


def _item_snippets(root, item_selector: Optional[str], k: int = 2) -> str:
    import lxml.html as LH
    if not item_selector:
        return prune_html(LH.tostring(root, encoding="unicode"), 6000)
    nodes = root.cssselect(item_selector)[:k]
    return "\n".join(prune_html(LH.tostring(n, encoding="unicode"), 2500)
                     for n in nodes)


def compile_source(spec: SourceSpec, html: str, base_url: str,
                   client: Optional[OllamaClient],
                   on_round: Optional[Callable] = None) -> CompileOutcome:
    root = parse_html(html)
    outcome = CompileOutcome(lock=None)
    item_sel = _pick_item_selector(spec, root, base_url)
    lock = SourceLock(compiled_at=now_iso(), item_selector=item_sel)
    llm_calls: list = []

    snippets = None
    for name, fspec in spec.fields.items():
        # manual selector in the source yml always wins
        if fspec.selector:
            sel: Optional[SelectorDef] = SelectorDef(css=fspec.selector,
                                                     attr=fspec.attr)
            via = "manual"
            fallbacks: list[SelectorDef] = []
        else:
            # proposer 1: heuristics (ms, no LLM)
            sel, via, fallbacks = None, "", []
            for cand in field_candidates(root, item_sel, fspec.ftype, fspec.desc,
                                         name, base_url):
                pr = probe_selector(root, item_sel, cand, fspec.ftype, base_url)
                if _passes(pr, fspec.ftype.nullable):
                    if sel is None:
                        sel, via = cand, "heuristic"
                    elif len(fallbacks) < 2 and cand.css != sel.css:
                        fallbacks.append(cand)     # free fallback diversity
            # proposer 2: the LLM, one field per call, only if needed
            if sel is None and client is not None:
                if snippets is None:
                    snippets = _item_snippets(root, item_sel)
                if on_round:
                    on_round(len(llm_calls) + 1, "llm_field", {"field": name})
                sel = llm_field_selector(client, root, item_sel, spec, name,
                                         base_url, snippets, counter=llm_calls)
                via = "llm"
        if sel is None:
            outcome.failed_fields.append(name)
            continue
        pr = probe_selector(root, item_sel, sel, fspec.ftype, base_url)
        fp = fingerprint_from_nodes(pr["nodes"], pr["raws"])
        lock.fields[name] = FieldLock(selector=sel, fallbacks=fallbacks,
                                      fingerprint=fp, status=STATUS_VERIFIED,
                                      verified_at=now_iso())
        outcome.via[name] = via
        outcome.per_field[name] = {
            "selector": sel.pretty(), "via": via,
            "matched": pr["matched_scopes"], "scopes": pr["scopes"],
            "fallbacks": len(fallbacks),
            "samples": [v for v in pr["values"][:3] if v is not None],
        }
    outcome.rounds = len(llm_calls)
    outcome.lock = lock if lock.fields else None
    return outcome
