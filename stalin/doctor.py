"""`stalin doctor` — environment check. Tells you exactly what will and won't work."""
from __future__ import annotations

import httpx

from .config import Project
from .llm import OllamaClient
from .ux import FAIL, OK, WARN, err_console


def run_doctor(project: Project | None) -> int:
    ok = True

    def line(good, msg, warn=False):
        nonlocal ok
        mark = OK if good else (WARN if warn else FAIL)
        if not good and not warn:
            ok = False
        err_console.print(f"  {mark} {msg}")

    # project
    if project:
        names = project.source_names()
        line(True, f"project at {project.root} ({len(names)} source(s): "
                   f"{', '.join(names) or '—'})")
        from .lockfile import Lock
        lock = Lock.load(project.lock_path)
        for n in names:
            sl = lock.sources.get(n)
            if sl is None:
                line(False, f"source {n!r} has no compiled selectors — `stalin heal {n}` "
                     "or re-run `stalin add`", warn=True)
            else:
                bad = [f for f, fl in sl.fields.items() if fl.status == "broken"]
                line(not bad, f"source {n!r}: {len(sl.fields)} fields compiled"
                     + (f", broken: {', '.join(bad)}" if bad else ""), warn=bool(bad))
    else:
        line(False, "no stalin.yml here — `stalin init` to create a project", warn=True)

    # network
    try:
        r = httpx.get("https://example.com", timeout=8,
                      headers={"User-Agent": "stalin-doctor"})
        line(r.status_code < 500, f"outbound https ok ({r.status_code})")
    except httpx.HTTPError as e:
        line(False, f"outbound https failed: {e}")

    # ollama
    cfg = project.config.llm if project else None
    base = cfg.base_url if cfg else "http://localhost:11434"
    try:
        tags = httpx.get(f"{base}/api/tags", timeout=5).json()
        models = [m["name"] for m in tags.get("models", [])]
        line(True, f"ollama reachable at {base} ({len(models)} model(s))")
        try:
            client = OllamaClient.create(cfg) if cfg else None
            if client:
                line(True, f"tool-capable model: [bold]{client.model}[/bold] — healing enabled")
        except Exception as e:
            line(False, f"no tool-capable model: {e}", warn=True)
            err_console.print("      [dim]stalin still works: drift detection + alerts, "
                              "no auto-heal (rung 2 skipped)[/dim]")
    except Exception:
        line(False, "ollama not reachable — healing disabled (detection-only mode)",
             warn=True)
        err_console.print("      [dim]install: https://ollama.com · then "
                          "`ollama pull qwen3`[/dim]")
    return 0 if ok else 1
