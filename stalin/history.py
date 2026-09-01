"""Append-only heal/selector event log: .stalin/history/<source>.jsonl.

Committed, one JSON object per line — git-friendly, trivially diffable.
"""
from __future__ import annotations

import json
from pathlib import Path

from .utils import now_iso


def append(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {"ts": now_iso(), **event}
    with path.open("a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def read(path: Path, field: str | None = None) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if field and ev.get("field") != field:
            continue
        events.append(ev)
    return events
