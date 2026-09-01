"""Acceptance gates: 100% deterministic candidate-selector judgment.

The LLM proposes; these gates dispose. A healed selector is only accepted if
every gate passes — no model opinion is ever trusted about its own output.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from .config import FieldSpec
from .extract import probe_selector
from .fingerprint import (Fingerprint, fingerprint_from_nodes, robustness_score,
                          shape_match_ratio, value_shape)
from .lockfile import SelectorDef


@dataclass
class GateResult:
    passed: bool
    detail: dict = dc_field(default_factory=dict)
    probe: dict = dc_field(default_factory=dict)
    robustness: float = 0.0

    def summary(self) -> dict:
        return {**{k: v for k, v in self.detail.items()},
                "robustness": round(self.robustness, 2)}


def run_gates(root, item_selector, sel: SelectorDef, fspec: FieldSpec,
              fp: Fingerprint, base_url: str, min_items: int) -> GateResult:
    ftype = fspec.ftype
    pr = probe_selector(root, item_selector, sel, ftype, base_url)
    detail: dict = {}
    n = max(pr["scopes"], 1)

    # gate 1: schema — every match casts (nulls per contract)
    detail["schema"] = pr["cast_fail"] == 0
    # gate 2: cardinality — near the historical baseline, and enough coverage
    base = fp.match_count_ema
    matched = pr["matched_scopes"]
    if base and base >= 1:
        ratio = matched / base
        detail["cardinality"] = 0.5 <= ratio <= 2.0 and matched >= min(min_items, n)
        detail["cardinality_ratio"] = round(ratio, 2)
    else:
        detail["cardinality"] = matched > 0 or ftype.nullable
    # gate 3: shape — old shape, or a consistent new one (shape migration)
    vals = [r for r in pr["raws"] if isinstance(r, str)]
    if vals and fp.value_shape:
        old_ratio = shape_match_ratio(fp.value_shape, vals)
        if old_ratio >= 0.7:
            detail["shape"] = True
        else:
            new_shape = value_shape(vals[:20])
            migrated = shape_match_ratio(new_shape, vals) >= 0.9
            detail["shape"] = migrated
            if migrated:
                detail["shape_migrated_to"] = new_shape
    else:
        detail["shape"] = True
    # gate 4: anchor — if an old sample survives on the page, we must capture
    # it EXACTLY. Substring matching would bless a container that merely
    # contains the old value ("<title> 160 karma · 214 replies").
    import re as _re

    def _norm(t: str) -> str:
        return _re.sub(r"\s+", " ", t).strip()[:80]

    page_text = " ".join(root.itertext()) if hasattr(root, "itertext") else ""
    page_norm = _norm.__call__(page_text) if False else _re.sub(r"\s+", " ", page_text)
    anchor_checked = False
    anchor_ok = True
    for sample in fp.samples:
        if sample and _norm(sample) and _norm(sample) in page_norm:
            anchor_checked = True
            anchor_ok = any(isinstance(r, str) and _norm(r) == _norm(sample)
                            for r in pr["raws"])
            if anchor_ok:
                break
    detail["anchor"] = anchor_ok if anchor_checked else None   # None = not applicable
    # gate 5: robustness
    rob = robustness_score(sel.css)
    detail["robust"] = rob >= 0.3

    hard_gates = [detail["schema"], detail["cardinality"], detail["shape"],
                  detail["robust"]]
    if anchor_checked:
        hard_gates.append(anchor_ok)
    return GateResult(passed=all(hard_gates), detail=detail, probe=pr, robustness=rob)


def fingerprint_after_accept(pr: dict) -> Fingerprint:
    return fingerprint_from_nodes(pr["nodes"], pr["raws"])
