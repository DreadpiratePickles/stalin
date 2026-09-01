"""Deterministic field fingerprints: value shapes, node signatures, EMA stats.

Nothing in this module touches an LLM. These are the cheap signals that let
drift detection run in microseconds on every scrape.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Optional

from pydantic import BaseModel, Field

from .utils import ema

_HASHY_CLASS = re.compile(r"(?:^css-|^sc-|[a-f0-9]{6,}$|^[a-z]+-[A-Za-z0-9]{5,8}$)")


class Fingerprint(BaseModel):
    tag: str = ""
    class_tokens: list[str] = Field(default_factory=list)
    ancestor_sig: str = ""
    value_shape: str = ""
    match_count_ema: Optional[float] = None
    null_rate_ema: Optional[float] = None
    len_mean: Optional[float] = None
    samples: list[str] = Field(default_factory=list)     # <=3, <=80 chars each


def value_shape(samples: Iterable[str]) -> str:
    """Learn a generalizing regex from sample values.

    '312 points' -> r'^\\d+ [A-Za-z]+$'. Uses the most common per-sample
    pattern; the >=70% match-ratio gate absorbs the rest.
    """
    pats = Counter()
    for s in samples:
        if not s:
            continue
        s = s.strip()[:120]
        out, prev = [], None
        for ch in s:
            if ch.isdigit():
                cls = r"\d+"
            elif ch.isalpha():
                cls = "[^\\W\\d_]+"
            elif ch.isspace():
                cls = r"\s+"
            else:
                cls = re.escape(ch)
            if cls != prev or not cls.endswith("+"):
                out.append(cls)
            prev = cls
        pats["^" + "".join(out) + "$"] += 1
    if not pats:
        return ""
    return pats.most_common(1)[0][0]


def shape_match_ratio(pattern: str, values: Iterable[str]) -> float:
    vals = [v for v in values if v is not None]
    if not pattern or not vals:
        return 1.0
    try:
        rx = re.compile(pattern)
    except re.error:
        return 1.0
    hits = sum(1 for v in vals if rx.match(v.strip()[:120]))
    return hits / len(vals)


def node_signature(el) -> tuple[str, list[str], str]:
    """(tag, class tokens, ancestor signature 'td.subtext<tr<table') for an lxml element."""
    tag = str(el.tag) if isinstance(el.tag, str) else ""
    classes = sorted((el.get("class") or "").split())
    sig_parts = []
    p = el.getparent()
    depth = 0
    while p is not None and depth < 3 and isinstance(p.tag, str):
        pc = sorted((p.get("class") or "").split())
        sig_parts.append(p.tag + ("." + pc[0] if pc else ""))
        p = p.getparent()
        depth += 1
    return tag, classes, "<".join(sig_parts)


def fingerprint_from_nodes(nodes, raw_values: list[Optional[str]]) -> Fingerprint:
    tags = Counter()
    classes: Counter = Counter()
    sigs = Counter()
    for el in nodes[:50]:
        t, c, s = node_signature(el)
        tags[t] += 1
        for tok in c:
            classes[tok] += 1
        sigs[s] += 1
    vals = [v for v in raw_values if v]
    n = len(raw_values) or 1
    # a shape is only stored if it actually generalizes its own training
    # samples — free-text fields (titles, names) get no shape and are judged
    # by the other signals instead
    shape = value_shape(vals[:20])
    if shape and shape_match_ratio(shape, vals[:20]) < 0.7:
        shape = ""
    return Fingerprint(
        tag=tags.most_common(1)[0][0] if tags else "",
        class_tokens=[c for c, _ in classes.most_common(5)],
        ancestor_sig=sigs.most_common(1)[0][0] if sigs else "",
        value_shape=shape,
        match_count_ema=float(len([v for v in raw_values if v is not None])),
        null_rate_ema=len([v for v in raw_values if v is None]) / n,
        len_mean=(sum(len(v) for v in vals) / len(vals)) if vals else None,
        samples=[v[:80] for v in vals[:3]],
    )


def node_similarity(fp: Fingerprint, nodes) -> float:
    """Jaccard-ish similarity between stored fingerprint and freshly matched nodes."""
    if not nodes:
        return 0.0
    t, c, s = node_signature(nodes[0])
    score = 0.0
    score += 0.4 if t == fp.tag else 0.0
    a, b = set(c), set(fp.class_tokens)
    if a or b:
        score += 0.4 * (len(a & b) / len(a | b))
    else:
        score += 0.4
    sa, sb = set(s.split("<")), set(fp.ancestor_sig.split("<"))
    if sa or sb:
        score += 0.2 * (len(sa & sb) / len(sa | sb))
    else:
        score += 0.2
    return score


def update_stats(fp: Fingerprint, match_count: int, null_count: int,
                 raw_values: list[Optional[str]]) -> None:
    n = len(raw_values) or 1
    fp.match_count_ema = ema(fp.match_count_ema, float(match_count))
    fp.null_rate_ema = ema(fp.null_rate_ema, null_count / n)
    vals = [v for v in raw_values if v]
    if vals:
        fp.len_mean = ema(fp.len_mean, sum(len(v) for v in vals) / len(vals))
        fp.samples = [v[:80] for v in vals[:3]]


def robustness_score(css: str) -> float:
    """0..1. Penalize position-brittle and hash-classed selectors."""
    score = 1.0
    depth = css.count(">") + css.count(" ")
    if depth > 6:
        score -= 0.3
    nth = css.count(":nth-child") + css.count(":nth-of-type")
    if nth >= 1:
        score -= 0.2 * nth
    for m in re.finditer(r"\.([\w-]+)", css):
        if _HASHY_CLASS.search(m.group(1)):
            score -= 0.4
    if re.search(r"\[id\^?=", css) or "#" in css:
        score += 0.05
    if not re.search(r"[.#\[]", css):
        score -= 0.35              # bare tags ride position, not identity
    return max(0.0, min(1.0, score))
