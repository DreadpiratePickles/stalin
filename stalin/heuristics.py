"""Deterministic selector candidate generation. No LLM anywhere in this file.

The council pattern: many cheap proposers generate candidates, the gates are
the chairman that judges them. The LLM is just one more proposer — consulted
only for the hard tail the heuristics can't cover. On most pages, this module
compiles and heals everything by itself in milliseconds.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Optional

from .fingerprint import Fingerprint, robustness_score, shape_match_ratio
from .lockfile import SelectorDef
from .schema import CastError, FieldType

_STOP = {"the", "a", "an", "of", "each", "every", "to", "it", "that", "this",
         "points", "number", "count", "text", "link", "value"}


def _keywords(desc: str, name: str) -> set[str]:
    words = set(re.findall(r"[a-z]+", (desc + " " + name).lower()))
    return (words - _STOP) | {name.lower()}


def _classes(el) -> list[str]:
    return (el.get("class") or "").split()


def item_selector_candidates(root, min_items: int) -> list[str]:
    """Find repeating structures: (tag, class) combos that occur many times
    with children, ranked by count and shallowness."""
    counts: Counter = Counter()
    depth_sum: dict = defaultdict(int)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for c in _classes(el):
            if len(el) > 0:                       # has children -> container-ish
                key = (el.tag, c)
                counts[key] += 1
                d = 0
                p = el
                while p.getparent() is not None:
                    p = p.getparent()
                    d += 1
                depth_sum[key] += d
    out = []
    for (tag, c), n in counts.items():
        if n < max(min_items, 3):
            continue
        css = f"{tag}.{c}"
        rob = robustness_score(css)
        if rob < 0.3:
            continue
        avg_depth = depth_sum[(tag, c)] / n
        out.append((css, n, avg_depth, rob))
    # prefer more matches, shallower, more robust
    out.sort(key=lambda x: (-x[1], x[2], -x[3]))
    return [css for css, *_ in out[:8]]


def _selector_for(el, scope) -> list[str]:
    """Generate a few CSS selector spellings that reach `el` from `scope`."""
    cands = []
    tag = el.tag
    classes = _classes(el)
    for c in classes:
        cands.append(f".{c}")
        cands.append(f"{tag}.{c}")
    if el.get("id"):
        cands.append(f"#{el.get('id')}")
    # parent-qualified variants
    p = el.getparent()
    if p is not None and isinstance(p.tag, str):
        for pc in _classes(p)[:2]:
            for c in classes[:1]:
                cands.append(f".{pc} .{c}")
            if not classes:
                cands.append(f".{pc} {tag}")
    if not classes and p is not None:
        cands.append(tag)
        if isinstance(p.tag, str):
            cands.append(f"{p.tag} > {tag}")
    return cands


def _try_cast(ftype: FieldType, raw, base_url: str) -> bool:
    try:
        ftype.cast(raw, base_url)
        return True
    except CastError:
        return False


def field_candidates(root, item_selector: Optional[str], ftype: FieldType,
                     desc: str, name: str, base_url: str,
                     fp: Optional[Fingerprint] = None,
                     max_cands: int = 12) -> list[SelectorDef]:
    """Rank candidate selectors for one field.

    Scoring blends: coverage across items, type fit, keyword affinity between
    the field description and class names, value-shape match against a stored
    fingerprint (heals), and selector robustness.
    """
    scopes = root.cssselect(item_selector) if item_selector else [root]
    if not scopes:
        return []
    probe_scopes = scopes[: min(len(scopes), 8)]
    kw = _keywords(desc, name)
    wants_href = ftype.base == "url"

    # collect (selector spelling -> stats) across probe scopes
    stats: dict[tuple[str, str], dict] = {}
    for scope in probe_scopes:
        seen_in_scope: set = set()
        for el in scope.iter():
            if not isinstance(el.tag, str):
                continue
            txt = " ".join(t.strip() for t in el.itertext() if t.strip())[:200]
            values = []
            if wants_href and el.get("href"):
                values.append(("href", el.get("href")))
            is_leaf = len(el) == 0
            short = len(txt) <= 60
            # numeric/enum/bool/date fields live in true leaves or short spans;
            # only free-text (str) may come from small containers
            if txt and (is_leaf or (ftype.base == "str" and len(el) <= 2)
                        or (short and len(el) <= 2)):
                values.append(("text", txt))
            for attr, raw in values:
                if not _try_cast(ftype, raw, base_url):
                    continue
                for css in _selector_for(el, scope):
                    key = (css, attr)
                    if key in seen_in_scope:
                        continue
                    seen_in_scope.add(key)
                    st = stats.setdefault(key, {"cover": 0, "raws": [], "leaf": 0})
                    st["cover"] += 1
                    st["raws"].append(raw)
                    if is_leaf or attr != "text":
                        st["leaf"] += 1

    scored = []
    n = len(probe_scopes)
    for (css, attr), st in stats.items():
        coverage = st["cover"] / n
        if coverage < (0.3 if ftype.nullable else 0.6):
            continue
        score = coverage * 2.0
        score += robustness_score(css)
        score -= 0.02 * len(css)
        # prefer true leaves: a container's text may cast today ("42 pts · 7
        # cmts" regex-extracts 42) but breaks the moment the order changes
        score += 0.6 * (st.get("leaf", 0) / max(st["cover"], 1))
        # keyword affinity: class tokens vs description words
        toks = set(re.findall(r"[a-z]+", css.lower()))
        overlap = len(toks & kw)
        score += 0.8 * overlap
        # fingerprint shape match (healing): does it look like the old values?
        if fp and fp.value_shape:
            ratio = shape_match_ratio(fp.value_shape, st["raws"])
            score += 1.5 * ratio
            if fp.samples and any(s in st["raws"] for s in fp.samples):
                score += 2.0                       # anchor: old value found again
        if fp and fp.class_tokens:
            score += 0.5 * len(toks & set(fp.class_tokens))
        scored.append((score, SelectorDef(css=css, attr=attr)))

    scored.sort(key=lambda x: -x[0])
    seen_css = set()
    out = []
    for _, sel in scored:
        if sel.css in seen_css:
            continue
        seen_css.add(sel.css)
        out.append(sel)
        if len(out) >= max_cands:
            break
    return out
