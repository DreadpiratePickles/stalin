"""Selector execution against parsed HTML. Pure, deterministic, fast.

The LLM proposes selectors; this module is the executor that tells the truth
about what they actually match.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import lxml.html

from .config import SourceSpec
from .lockfile import SelectorDef, SourceLock
from .schema import CastError


def parse_html(html: str):
    try:
        return lxml.html.fromstring(html)
    except Exception:
        # tolerant fallback for broken markup
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        return lxml.html.fromstring(str(soup))


def _node_value(el, attr: str) -> Optional[str]:
    if attr == "text":
        txt = " ".join(t.strip() for t in el.itertext() if t.strip())
        return txt or None
    v = el.get(attr)
    return v.strip() if v else None


def run_selector(root, sel: SelectorDef) -> tuple[list, list[Optional[str]]]:
    """Execute one selector. Returns (matched nodes, raw string values after post)."""
    try:
        nodes = root.cssselect(sel.css)
    except Exception:
        return [], []
    raws: list[Optional[str]] = []
    for el in nodes:
        v = _node_value(el, sel.attr)
        for op in sel.post:
            if v is None:
                break
            if op.startswith("regex:"):
                m = re.search(op[6:], v)
                v = m.group(1) if (m and m.groups()) else (m.group(0) if m else None)
            elif op == "strip":
                v = v.strip()
        raws.append(v)
    return nodes, raws


@dataclass
class FieldStats:
    match_count: int = 0
    null_count: int = 0
    cast_fail: int = 0
    raws: list = field(default_factory=list)
    nodes: list = field(default_factory=list)
    values: list = field(default_factory=list)      # cast values (None on failure)


@dataclass
class ExtractResult:
    items: list[dict]
    stats: dict[str, FieldStats]
    item_count: int


def extract(html_or_root, lock: SourceLock, spec: SourceSpec,
            base_url: str) -> ExtractResult:
    """Full extraction: item_selector -> per-item field selectors -> cast values."""
    root = html_or_root if not isinstance(html_or_root, str) else parse_html(html_or_root)
    if lock.item_selector:
        try:
            items_nodes = root.cssselect(lock.item_selector)
        except Exception:
            items_nodes = []
    else:
        items_nodes = [root]

    stats = {name: FieldStats() for name in spec.fields}
    rows: list[dict] = []
    for item_el in items_nodes:
        row: dict[str, Any] = {}
        for name, fspec in spec.fields.items():
            fl = lock.fields.get(name)
            if fl is None:
                row[name] = None
                continue
            nodes, raws = run_selector(item_el, fl.selector)
            st = stats[name]
            st.match_count += len(nodes)
            st.nodes.extend(nodes[:2])
            ft = fspec.ftype
            if ft.is_list:
                raw: Any = [r for r in raws if r]
                st.raws.extend(raw[:2])
            else:
                raw = raws[0] if raws else None
                st.raws.append(raw)
            if raw is None or raw == []:
                st.null_count += 1
            try:
                val = ft.cast(raw, base_url)
            except CastError:
                st.cast_fail += 1
                val = None
            row[name] = val
            st.values.append(val)
        rows.append(row)
    return ExtractResult(items=rows, stats=stats, item_count=len(items_nodes))


def probe_selector(html_or_root, item_selector: Optional[str], sel: SelectorDef,
                   ftype, base_url: str) -> dict:
    """Try one candidate selector; report what the gates will see.

    Used both by the LLM tool loop (as live feedback) and the heal gates.
    """
    root = html_or_root if not isinstance(html_or_root, str) else parse_html(html_or_root)
    scopes = root.cssselect(item_selector) if item_selector else [root]
    per_scope_raws: list[Optional[str]] = []
    all_nodes: list = []
    cast_ok = 0
    cast_fail = 0
    values: list = []
    for scope in scopes:
        nodes, raws = run_selector(scope, sel)
        all_nodes.extend(nodes[:2])
        if ftype and ftype.is_list:
            raw: Any = [r for r in raws if r]
        else:
            raw = raws[0] if raws else None
        per_scope_raws.append(raw if not isinstance(raw, list) else (raw[0] if raw else None))
        try:
            v = ftype.cast(raw, base_url) if ftype else raw
            cast_ok += 1
            values.append(v)
        except CastError as e:
            cast_fail += 1
            values.append(None)
    matched_scopes = sum(1 for r in per_scope_raws if r is not None)
    return {
        "scopes": len(scopes),
        "matched_scopes": matched_scopes,
        "cast_ok": cast_ok,
        "cast_fail": cast_fail,
        "raws": per_scope_raws,
        "values": values,
        "nodes": all_nodes,
    }
