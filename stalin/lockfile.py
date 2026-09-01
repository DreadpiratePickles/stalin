"""Compiled state: .stalin/lock.json. Machine-owned, committed, reviewed in PRs.

Like a lockfile: only stalin writes it. `git diff .stalin/lock.json` is the
audit trail of every selector the healer has ever changed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .fingerprint import Fingerprint
from .utils import now_iso

STATUS_VERIFIED = "verified"
STATUS_UNCONFIRMED = "healed-unconfirmed"
STATUS_BROKEN = "broken"


class SelectorDef(BaseModel):
    css: str
    attr: str = "text"                 # "text" or an attribute name e.g. "href"
    post: list[str] = Field(default_factory=list)   # e.g. ["regex:(\\d+)"]

    def pretty(self) -> str:
        return self.css + (f" @{self.attr}" if self.attr != "text" else "")


class FieldLock(BaseModel):
    selector: SelectorDef
    fallbacks: list[SelectorDef] = Field(default_factory=list)
    fingerprint: Fingerprint = Field(default_factory=Fingerprint)
    status: str = STATUS_VERIFIED
    verified_at: str = ""
    heal_count: int = 0
    confirm_runs: int = 0              # clean runs since last heal (2 -> verified)


class SourceLock(BaseModel):
    compiled_at: str = ""
    item_selector: Optional[str] = None
    fields: dict[str, FieldLock] = Field(default_factory=dict)
    content_len_ema: Optional[float] = None
    status: str = STATUS_VERIFIED


class Lock(BaseModel):
    sources: dict[str, SourceLock] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Lock":
        if not path.exists():
            return cls()
        return cls.model_validate(json.loads(path.read_text()))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n")

    def require(self, source: str) -> SourceLock:
        if source not in self.sources:
            raise KeyError(
                f"source {source!r} has no compiled selectors — run `stalin add` "
                f"or `stalin heal {source}` to compile")
        return self.sources[source]


def promote_heal(fl: FieldLock, new_sel: SelectorDef) -> None:
    """Accepted heal: new selector becomes primary, old primary becomes fallback."""
    old = fl.selector
    fl.fallbacks = [old] + [f for f in fl.fallbacks if f.css != new_sel.css][:2]
    fl.selector = new_sel
    fl.status = STATUS_UNCONFIRMED
    fl.confirm_runs = 0
    fl.heal_count += 1
    fl.verified_at = now_iso()
