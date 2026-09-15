"""The Q-layer: filter / sort / select / paginate / search over extracted items.

One grammar, one engine, three surfaces (HTTP, MCP, CLI). It operates on the
typed item list *after* extraction, validation and healing — it never touches
selectors, fetches or snapshots, so self-healing is unaffected by construction.

Pipeline order (fixed, documented): filter → q → sort → paginate → project.
`count` is measured after filter+q, before pagination.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import datetime
from typing import Any, Optional

from .schema import CastError, FieldType

CONTROLS = ("sort", "fields", "limit", "offset", "q")
OPERATORS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "contains", "isnull")
MAX_LIMIT = 1000

_ORDER_TYPES = ("int", "float", "datetime")
_TEXTY = ("str", "url", "enum")


class QueryError(Exception):
    def __init__(self, param: str, code: str, detail: str = "",
                 expected_type: str = ""):
        self.param = param
        self.code = code
        self.detail = detail
        self.expected_type = expected_type
        super().__init__(detail or code)

    def as_dict(self) -> dict:
        d = {"error": "invalid_query", "param": self.param, "code": self.code}
        if self.expected_type:
            d["expected_type"] = self.expected_type
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass
class Filter:
    field: str
    op: str
    raw: Any
    coerced: Any = None            # filter value(s) coerced to the field type


@dataclass
class Query:
    filters: list[Filter] = dc_field(default_factory=list)
    sort: list[tuple[str, bool]] = dc_field(default_factory=list)   # (field, desc)
    fields: Optional[list[str]] = None
    limit: Optional[int] = None
    offset: int = 0
    q: Optional[str] = None

    def echo(self) -> dict:
        return {
            "filters": {f"{f.field}__{f.op}" if f.op != "eq" else f.field: f.raw
                        for f in self.filters},
            "q": self.q,
            "sort": ",".join(("-" if d else "") + n for n, d in self.sort) or None,
            "fields": self.fields,
            "limit": self.limit,
            "offset": self.offset,
        }


@dataclass
class QueryResult:
    items: list[dict]
    matched: int                   # after filter+q, before pagination


# --- parsing --------------------------------------------------------------

def _coerce(field: str, op: str, ft: FieldType, raw: str) -> Any:
    """Coerce a filter value to the field's type; raise typed QueryError."""
    if op == "isnull":
        s = str(raw).strip().lower()
        if s not in ("true", "false", "1", "0"):
            raise QueryError(f"{field}__isnull", "invalid_value",
                             f"expected true/false, got {raw!r}", "bool")
        return s in ("true", "1")
    if op == "in":
        parts = raw.split(",") if isinstance(raw, str) else list(raw)
        out = []
        for p in parts:
            out.append(_coerce_scalar(field, op, ft, p))
        return out
    return _coerce_scalar(field, op, ft, raw)


def _coerce_scalar(field: str, op: str, ft: FieldType, raw: Any) -> Any:
    key = f"{field}__{op}" if op != "eq" else field
    try:
        # list[str] fields compare against a single string element
        base = FieldType(ft.base, False, ft.enum_values) if ft.is_list else \
            FieldType(ft.base, False, ft.enum_values)
        return base.cast(str(raw))
    except CastError as e:
        raise QueryError(key, "invalid_value", str(e), ft.spec)


def parse_query(params: dict, schema: dict[str, FieldType],
                lookup_params: set[str]) -> tuple[Query, dict]:
    """Split a flat param map into (Query, lookup_args) per the precedence rule:

    1. reserved control word           2. `field__op` filter
    3. bare key == declared lookup param   4. bare key == schema field (eq)
    5. otherwise -> unknown_parameter
    """
    query = Query()
    lookup_args: dict = {}
    sort_seen = False
    for key, value in params.items():
        # 1. controls
        if key in CONTROLS:
            _apply_control(query, key, value, schema)
            continue
        # 2. operator-suffixed filters
        if "__" in key:
            field, _, op = key.rpartition("__")
            if op not in OPERATORS:
                raise QueryError(key, "unknown_operator",
                                 f"operator '__{op}' not in {list(OPERATORS)}")
            if field not in schema:
                raise QueryError(key, "unknown_field",
                                 f"'{field}' is not a field (have: {list(schema)})")
            _add_filter(query, field, op, value, schema[field])
            continue
        # 3. declared lookup param
        if key in lookup_params:
            lookup_args[key] = value
            continue
        # 4. bare schema field -> equality
        if key in schema:
            _add_filter(query, key, "eq", value, schema[key])
            continue
        # 5. unknown
        raise QueryError(key, "unknown_parameter",
                         f"'{key}' is not a control, filter, lookup param or field")
    return query, lookup_args


def _add_filter(query: Query, field: str, op: str, raw: Any, ft: FieldType) -> None:
    key = f"{field}__{op}" if op != "eq" else field
    # operator/type compatibility
    if op in ("gt", "gte", "lt", "lte") and ft.base not in _ORDER_TYPES:
        raise QueryError(key, "operator_not_supported_for_type",
                         f"'__{op}' needs an orderable field (int/float/datetime), "
                         f"not {ft.spec}", ft.spec)
    if op == "contains" and not (ft.is_list or ft.base in ("str", "url")):
        raise QueryError(key, "operator_not_supported_for_type",
                         f"'__contains' needs str/url/list, not {ft.spec}", ft.spec)
    coerced = None if op == "isnull" and False else _coerce(field, op, ft, raw)
    query.filters.append(Filter(field=field, op=op, raw=raw, coerced=coerced))


def _apply_control(query: Query, key: str, value: str, schema: dict) -> None:
    if key == "sort":
        for tok in str(value).split(","):
            tok = tok.strip()
            if not tok:
                continue
            desc = tok.startswith("-")
            name = tok[1:] if desc else tok
            if name not in schema:
                raise QueryError("sort", "invalid_control",
                                 f"cannot sort by unknown field '{name}'")
            query.sort.append((name, desc))
    elif key == "fields":
        names = [t.strip() for t in str(value).split(",") if t.strip()]
        for n in names:
            if n not in schema:
                raise QueryError("fields", "invalid_control",
                                 f"unknown field '{n}'")
        query.fields = names
    elif key == "limit":
        query.limit = _int_control("limit", value, lo=0, hi=MAX_LIMIT)
    elif key == "offset":
        query.offset = _int_control("offset", value, lo=0)
    elif key == "q":
        query.q = str(value)


def _int_control(name: str, value: str, lo: int = 0, hi: int | None = None) -> int:
    try:
        n = int(str(value))
    except ValueError:
        raise QueryError(name, "invalid_control", f"{name} must be an integer")
    if n < lo or (hi is not None and n > hi):
        raise QueryError(name, "invalid_control",
                         f"{name} must be in [{lo}, {hi if hi is not None else '∞'}]")
    return n


# --- application ----------------------------------------------------------

def _as_datetime(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _cmp_key(ft: FieldType, v: Any) -> Any:
    if ft.base == "datetime":
        return _as_datetime(v)
    if ft.base in ("int", "float"):
        return float(v)
    return v


def _match(item: dict, f: Filter, ft: FieldType) -> bool:
    stored = item.get(f.field)
    if f.op == "isnull":
        return (stored is None) == bool(f.coerced)
    if stored is None:
        return False                       # SQL-like: null compares false
    if f.op == "contains":
        if ft.is_list:
            return f.coerced in (stored or [])
        return str(f.coerced).lower() in str(stored).lower()
    if f.op == "in":
        return stored in f.coerced
    if f.op == "eq":
        return stored == f.coerced
    if f.op == "ne":
        return stored != f.coerced
    a, b = _cmp_key(ft, stored), _cmp_key(ft, f.coerced)
    if a is None or b is None:
        return False
    if f.op == "gt":
        return a > b
    if f.op == "gte":
        return a >= b
    if f.op == "lt":
        return a < b
    if f.op == "lte":
        return a <= b
    return False


def _q_match(item: dict, needle: str, schema: dict[str, FieldType]) -> bool:
    n = needle.lower()
    for name, ft in schema.items():
        if ft.base not in _TEXTY:
            continue
        v = item.get(name)
        if v is None:
            continue
        if ft.is_list:
            if any(n in str(e).lower() for e in v):
                return True
        elif n in str(v).lower():
            return True
    return False


def apply(items: list[dict], query: Query,
          schema: dict[str, FieldType]) -> QueryResult:
    rows = items
    for f in query.filters:
        ft = schema[f.field]
        rows = [it for it in rows if _match(it, f, ft)]
    if query.q:
        rows = [it for it in rows if _q_match(it, query.q, schema)]
    matched = len(rows)
    for name, desc in reversed(query.sort):     # stable multi-key
        ft = schema[name]
        null_rank = -1 if desc else 1           # keeps nulls last under reverse=
        rows = sorted(
            rows,
            key=lambda it, n=name, t=ft, nr=null_rank: _sort_key(it.get(n), t, nr),
            reverse=desc,
        )
    if query.offset:
        rows = rows[query.offset:]
    if query.limit is not None:
        rows = rows[:query.limit]
    if query.fields is not None:
        rows = [{k: it.get(k) for k in query.fields} for it in rows]
    return QueryResult(items=rows, matched=matched)


def _sort_key(v: Any, ft: FieldType, null_rank: int = 1):
    """Total order that keeps nulls last regardless of sort direction.

    `null_rank` is chosen by the caller (-1 for desc, 1 for asc) so that after
    `sorted(reverse=desc)` the null bucket always lands at the tail.
    """
    if v is None:
        return (null_rank, 0.0)
    if ft.base == "datetime":
        dt = _as_datetime(v)
        return (0, dt.timestamp() if dt else 0.0)
    if ft.base in ("int", "float"):
        return (0, float(v))
    if ft.is_list:
        return (0, ",".join(map(str, v)))
    return (0, str(v).lower())


# --- envelope integration -------------------------------------------------

def query_envelope(base: dict, query: Query,
                   schema: dict[str, FieldType]) -> dict:
    """Apply a parsed Query to an existing result envelope's items.

    Additive: for an empty query, `count == returned == len(items)` and the
    only new keys are `returned` and `_query` — no existing consumer breaks.
    """
    items = base.get("items", []) or []
    result = apply(items, query, schema)
    env = dict(base)
    env["count"] = result.matched
    env["returned"] = len(result.items)
    env["items"] = result.items
    env["_query"] = query.echo()
    return env
