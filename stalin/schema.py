"""The type system: YAML type strings -> casters + pydantic models + JSON Schema.

Types: str, int, float, bool, url, datetime, list[str], enum[a,b,c] — any of
them nullable via `X | null`. The healer may change WHERE data comes from,
never WHAT SHAPE it has; this module is that shape.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse

from pydantic import BaseModel, create_model

_INT_RE = re.compile(r"[-+]?\d[\d,]*")
_FLOAT_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
_TRUE = {"true", "yes", "1", "y", "on"}
_FALSE = {"false", "no", "0", "n", "off"}


class CastError(ValueError):
    pass


def _cast_int(raw: str, base_url: str) -> int:
    s = raw.strip().replace(",", "")
    try:
        return int(s)
    except ValueError:
        m = _INT_RE.search(raw)
        if m:
            return int(m.group(0).replace(",", ""))
        raise CastError(f"not an int: {raw!r}")


def _cast_float(raw: str, base_url: str) -> float:
    s = raw.strip().replace(",", "").lstrip("$€£")
    try:
        return float(s)
    except ValueError:
        m = _FLOAT_RE.search(raw)
        if m:
            return float(m.group(0).replace(",", ""))
        raise CastError(f"not a float: {raw!r}")


def _cast_bool(raw: str, base_url: str) -> bool:
    s = raw.strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise CastError(f"not a bool: {raw!r}")


def _cast_url(raw: str, base_url: str) -> str:
    raw = raw.strip()
    # must LOOK like a link before urljoin makes anything "valid"
    if (" " in raw or not raw
            or not (raw.startswith(("http://", "https://", "/", "./", "../", "#", "?"))
                    or "://" in raw or raw.startswith("mailto:"))):
        raise CastError(f"not a url: {raw!r}")
    u = urljoin(base_url, raw)
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise CastError(f"not a url: {raw!r}")
    return u


def _cast_datetime(raw: str, base_url: str) -> str:
    s = raw.strip()
    for fmt in (None, "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d %b %Y", "%b %d, %Y"):
        try:
            dt = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
            return dt.isoformat()
        except ValueError:
            continue
    raise CastError(f"not a datetime: {raw!r}")


def _cast_str(raw: str, base_url: str) -> str:
    return re.sub(r"\s+", " ", raw).strip()


@dataclass(frozen=True)
class FieldType:
    """A parsed field type declaration."""
    base: str                      # str|int|float|bool|url|datetime|list[str]|enum
    nullable: bool
    enum_values: tuple[str, ...] = ()
    is_list: bool = False

    @property
    def spec(self) -> str:
        s = f"enum[{','.join(self.enum_values)}]" if self.base == "enum" else (
            "list[str]" if self.is_list else self.base)
        return f"{s} | null" if self.nullable else s

    def cast(self, raw: Optional[str | list[str]], base_url: str = "") -> Any:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if self.nullable:
                return None
            raise CastError("required field is empty")
        if self.is_list:
            vals = raw if isinstance(raw, list) else [raw]
            return [_cast_str(v, base_url) for v in vals if v and v.strip()]
        if isinstance(raw, list):          # scalar field, multiple nodes: take first
            raw = raw[0]
        if self.base == "enum":
            v = _cast_str(raw, base_url)
            for ev in self.enum_values:
                if v.lower() == ev.lower():
                    return ev
            raise CastError(f"{v!r} not in enum {list(self.enum_values)}")
        return _CASTERS[self.base](raw, base_url)

    def py_type(self) -> Any:
        t = {"str": str, "int": int, "float": float, "bool": bool,
             "url": str, "datetime": str, "enum": str}.get(self.base, str)
        if self.is_list:
            t = list[str]
        return Optional[t] if self.nullable else t


_CASTERS: dict[str, Callable[[str, str], Any]] = {
    "str": _cast_str, "int": _cast_int, "float": _cast_float,
    "bool": _cast_bool, "url": _cast_url, "datetime": _cast_datetime,
}

_ENUM_RE = re.compile(r"^enum\[([^\]]+)\]$")


def parse_type(text: str) -> FieldType:
    parts = [p.strip() for p in text.split("|")]
    nullable = "null" in parts or "none" in [p.lower() for p in parts]
    core = [p for p in parts if p.lower() not in ("null", "none")]
    if len(core) != 1:
        raise ValueError(f"bad type: {text!r}")
    t = core[0]
    if t == "list[str]":
        return FieldType("str", nullable, is_list=True)
    m = _ENUM_RE.match(t)
    if m:
        vals = tuple(v.strip() for v in m.group(1).split(","))
        return FieldType("enum", nullable, enum_values=vals)
    if t not in _CASTERS:
        raise ValueError(f"unknown type {t!r} (valid: {', '.join(_CASTERS)}, list[str], enum[a,b])")
    return FieldType(t, nullable)


def build_model(source_name: str, fields: dict[str, FieldType]) -> type[BaseModel]:
    defs: dict[str, Any] = {}
    for name, ft in fields.items():
        defs[name] = (ft.py_type(), None if ft.nullable else ...)
    return create_model(f"{source_name.title()}Item", **defs)


def json_schema(source_name: str, fields: dict[str, FieldType], version: str) -> dict:
    model = build_model(source_name, fields)
    sch = model.model_json_schema()
    sch["title"] = source_name
    sch["x-schema-version"] = version
    return sch


def schema_sha(sch: dict) -> str:
    return hashlib.sha256(json.dumps(sch, sort_keys=True).encode()).hexdigest()[:16]
