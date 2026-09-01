"""Cheap drift detection. No LLM, runs on every scrape, microseconds.

Five signals, evaluated in order; any trip marks the field drifted with a
named cause. The silent-breakage case competitors miss is #4: the selector
still matches SOMETHING, but it's the wrong something.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import DriftConfig, FieldSpec
from .extract import FieldStats
from .fingerprint import Fingerprint, node_similarity, shape_match_ratio
from .lockfile import FieldLock


@dataclass
class DriftReport:
    field: str
    causes: list[str] = field(default_factory=list)

    @property
    def drifted(self) -> bool:
        return bool(self.causes)


def check_field(name: str, fspec: FieldSpec, fl: FieldLock, st: FieldStats,
                item_count: int, min_items: int, cfg: DriftConfig) -> DriftReport:
    rep = DriftReport(field=name)
    fp: Fingerprint = fl.fingerprint
    loose = fspec.drift == "loose"
    n = max(item_count, 1)

    # 1. zero-match: matched nothing where history says we match plenty
    if st.match_count == 0 and (fp.match_count_ema or 0) > 0.5 and not fspec.ftype.nullable:
        rep.causes.append("zero-match")
        return rep
    if st.match_count == 0 and (fp.match_count_ema or 0) > 0.5 and fspec.ftype.nullable:
        # nullable field vanishing everywhere is still suspicious
        if (fp.null_rate_ema or 0) < 0.5:
            rep.causes.append("zero-match")
            return rep

    # 2. schema fail
    if n and st.cast_fail / n > cfg.schema_fail_ratio:
        rep.causes.append(f"schema-fail {st.cast_fail}/{n}")

    # 3. cardinality anomaly
    base = fp.match_count_ema
    if base and base >= 1:
        ratio = st.match_count / base
        lo = cfg.cardinality_low * (0.5 if loose else 1.0)
        hi = cfg.cardinality_high * (2.0 if loose else 1.0)
        if ratio < lo or ratio > hi:
            rep.causes.append(f"cardinality {ratio:.1f}x baseline")
    if item_count < min_items:
        rep.causes.append(f"items {item_count} < min_items {min_items}")

    # 4. shape anomaly: matches, but the wrong thing
    vals = [r for r in st.raws if isinstance(r, str)]
    if vals and fp.value_shape and not loose:
        ratio = shape_match_ratio(fp.value_shape, vals)
        if ratio < cfg.shape_min_ratio:
            rep.causes.append(f"shape {ratio:.0%} match")
    if fp.null_rate_ema is not None and n:
        null_rate = st.null_count / n
        if null_rate > 0.2 and null_rate > cfg.null_rate_mult * max(fp.null_rate_ema, 0.02):
            rep.causes.append(f"null-rate {null_rate:.0%}")

    # 5. fingerprint mismatch
    if st.nodes and not loose:
        sim = node_similarity(fp, st.nodes)
        if sim < cfg.fingerprint_min_sim:
            rep.causes.append(f"fingerprint {sim:.2f} sim")

    return rep


def check_source(spec, lock, result, cfg: DriftConfig) -> list[DriftReport]:
    reports = []
    for name, fspec in spec.fields.items():
        fl = lock.fields.get(name)
        if fl is None:
            continue
        rep = check_field(name, fspec, fl, result.stats[name],
                          result.item_count, spec.contract.min_items, cfg)
        if rep.drifted:
            reports.append(rep)
    return reports
