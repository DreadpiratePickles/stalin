"""Intent layer: stalin.yml + sources/*.yml. Human-owned, never touched by the healer."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator

from .schema import FieldType, parse_type

PROJECT_FILE = "stalin.yml"
SOURCES_DIR = "sources"
STATE_DIR = ".stalin"


class LLMConfig(BaseModel):
    provider: str = "ollama"
    model: str = "auto"
    base_url: str = "http://localhost:11434"


class ServeConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8411


class DriftConfig(BaseModel):
    """Thresholds for the cheap drift detectors."""
    cardinality_low: float = 0.5
    cardinality_high: float = 2.0
    shape_min_ratio: float = 0.70
    null_rate_mult: float = 3.0
    schema_fail_ratio: float = 0.20
    fingerprint_min_sim: float = 0.5


class Defaults(BaseModel):
    engine: str = "httpx"
    rate_limit: str = "1/2s"
    robots: str = "respect"          # respect | ignore
    timeout: str = "20s"


class ProjectConfig(BaseModel):
    version: int = 1
    defaults: Defaults = Field(default_factory=Defaults)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    serve: ServeConfig = Field(default_factory=ServeConfig)
    drift: DriftConfig = Field(default_factory=DriftConfig)


class FieldSpec(BaseModel):
    type: str = "str"
    desc: str = ""
    selector: Optional[str] = None      # manual override (still healed unless pinned)
    attr: str = "text"
    pin: bool = False
    drift: str = "normal"               # normal | loose

    @field_validator("type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        parse_type(v)
        return v

    @property
    def ftype(self) -> FieldType:
        return parse_type(self.type)


class ContractSpec(BaseModel):
    version: str = "1.0.0"
    min_items: int = 1


class SourceSpec(BaseModel):
    name: str
    url: str
    schedule: str = "15m"
    item: Optional[str] = None          # NL description of repeating unit; None = detail page
    fields: dict[str, FieldSpec]
    contract: ContractSpec = Field(default_factory=ContractSpec)
    engine: Optional[str] = None
    robots: Optional[str] = None

    @property
    def ftypes(self) -> dict[str, FieldType]:
        return {k: f.ftype for k, f in self.fields.items()}


class Project:
    """A stalin project rooted at `root` (dir containing stalin.yml)."""

    def __init__(self, root: Path):
        self.root = root
        self.config = self._load_config()

    @classmethod
    def find(cls, start: Path | None = None) -> "Project":
        cur = (start or Path.cwd()).resolve()
        for p in [cur, *cur.parents]:
            if (p / PROJECT_FILE).exists():
                return cls(p)
        raise FileNotFoundError(
            f"no {PROJECT_FILE} found here or above — run `stalin init` first")

    def _load_config(self) -> ProjectConfig:
        f = self.root / PROJECT_FILE
        data = yaml.safe_load(f.read_text()) or {}
        return ProjectConfig.model_validate(data)

    # --- paths -----------------------------------------------------------
    @property
    def sources_dir(self) -> Path:
        return self.root / SOURCES_DIR

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIR

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "lock.json"

    def history_path(self, source: str) -> Path:
        return self.state_dir / "history" / f"{source}.jsonl"

    def snapshot_dir(self, source: str) -> Path:
        return self.state_dir / "snapshots" / source

    def data_path(self, source: str) -> Path:
        return self.state_dir / "cache" / "last-good" / f"{source}.json"

    @property
    def http_cache_path(self) -> Path:
        return self.state_dir / "cache" / "http.json"

    # --- sources ---------------------------------------------------------
    def source_names(self) -> list[str]:
        if not self.sources_dir.exists():
            return []
        return sorted(p.stem for p in self.sources_dir.glob("*.yml"))

    def load_source(self, name: str) -> SourceSpec:
        f = self.sources_dir / f"{name}.yml"
        if not f.exists():
            raise FileNotFoundError(
                f"unknown source {name!r} (have: {', '.join(self.source_names()) or 'none'})")
        return SourceSpec.model_validate(yaml.safe_load(f.read_text()))

    def save_source(self, spec: SourceSpec) -> Path:
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        f = self.sources_dir / f"{spec.name}.yml"
        data = spec.model_dump(exclude_none=True, exclude_defaults=True)
        data["name"] = spec.name
        data["url"] = spec.url
        # keep field order and full field dicts readable
        data["fields"] = {
            k: {kk: vv for kk, vv in v.model_dump().items()
                if not (kk in ("selector",) and vv is None)
                and not (kk == "pin" and vv is False)
                and not (kk == "drift" and vv == "normal")
                and not (kk == "attr" and vv == "text")}
            for k, v in spec.fields.items()
        }
        data["contract"] = spec.contract.model_dump()
        f.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        return f


GITIGNORE_BLOCK = """# stalin (managed block)
.stalin/snapshots/
.stalin/cache/
data/
# /stalin (end managed block)
"""

PROJECT_TEMPLATE = """version: 1
defaults:
  engine: httpx
  rate_limit: 1/2s          # per host
  robots: respect
  timeout: 20s
llm:
  provider: ollama
  model: auto               # doctor picks the best tool-capable local model
  base_url: http://localhost:11434
serve:
  host: 127.0.0.1
  port: 8411
"""


def init_project(root: Path) -> list[Path]:
    """Scaffold a project. Returns created paths."""
    created = []
    pf = root / PROJECT_FILE
    if not pf.exists():
        pf.write_text(PROJECT_TEMPLATE)
        created.append(pf)
    for d in (root / SOURCES_DIR, root / STATE_DIR / "history",
              root / STATE_DIR / "snapshots", root / STATE_DIR / "cache" / "last-good",
              root / "data"):
        d.mkdir(parents=True, exist_ok=True)
    gi = root / ".gitignore"
    text = gi.read_text() if gi.exists() else ""
    if "# stalin (managed block)" not in text:
        gi.write_text(text + ("\n" if text and not text.endswith("\n") else "") + GITIGNORE_BLOCK)
        created.append(gi)
    return created
