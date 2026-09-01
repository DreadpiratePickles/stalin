"""Small shared helpers: durations, rates, time, ids."""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

_DUR_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)$")
_DUR_MULT = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(text: str | int | float) -> float:
    """'15m' -> 900.0 seconds. Bare numbers are seconds."""
    if isinstance(text, (int, float)):
        return float(text)
    m = _DUR_RE.match(text.strip())
    if not m:
        raise ValueError(f"bad duration: {text!r} (want e.g. '20s', '15m', '1h')")
    return float(m.group(1)) * _DUR_MULT[m.group(2)]


def parse_rate(text: str) -> float:
    """'1/2s' -> minimum 2.0s between requests. Returns min interval seconds."""
    m = re.match(r"^(\d+)\s*/\s*(\d+(?:\.\d+)?\s*(?:ms|s|m|h|d))$", text.strip())
    if not m:
        raise ValueError(f"bad rate: {text!r} (want e.g. '1/2s')")
    n = int(m.group(1))
    per = parse_duration(m.group(2))
    return per / max(n, 1)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def ema(old: float | None, new: float, alpha: float = 0.3) -> float:
    return new if old is None else (alpha * new + (1 - alpha) * old)
