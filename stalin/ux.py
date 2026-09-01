"""Terminal UX. Quiet on success; ceremony reserved for `add` and the heal."""
from __future__ import annotations

import json
import sys

from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)

OK = "[green]✓[/green]"
WARN = "[yellow]⚠[/yellow]"
FAIL = "[red]✗[/red]"


def is_tty() -> bool:
    return sys.stdout.isatty()


class UI:
    """Callbacks handed to the runner/healer for live progress."""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self._heal_lines: list = []

    def warn(self, msg: str) -> None:
        err_console.print(f"  {WARN} {msg}")

    # --- heal ceremony ---------------------------------------------------
    def drift_panel_start(self, source: str, rep, fl) -> None:
        self._heal_lines = []
        err_console.print()
        err_console.print(f"  {WARN} [bold yellow]drift detected[/bold yellow] · "
                          f"[bold]{source}.{rep.field}[/bold]")
        err_console.print(f"    selector  [cyan]{fl.selector.pretty()}[/cyan]")
        err_console.print(f"    signals   {' · '.join(rep.causes)}")

    def heal_progress(self, kind: str, data: dict) -> None:
        mark = OK if data.get("passed") else FAIL
        d = data.get("detail", {})
        gates = " ".join(
            f"{OK if v else FAIL} {k}" for k, v in d.items()
            if isinstance(v, bool))
        if kind == "fallback":
            err_console.print(f"    fallback  [cyan]{data['css']}[/cyan]  {mark}  {gates}")
        else:
            err_console.print(
                f"    candidate [cyan]{data['css']}[/cyan]  {mark}  "
                f"{data.get('matched', '?')}/{data.get('scopes', '?')} matched  {gates}")

    def heal_panel_end(self, ev) -> None:
        if ev.outcome == "healed":
            body = Group(
                Text.from_markup(
                    f"  [bold]{ev.field}[/bold]   [red strike]{ev.old}[/red strike]"
                    f"  ──→  [bold green]{ev.new}[/bold green]"),
                Text.from_markup(
                    f"  was: [dim]{ev.samples_before[:1] or ['?']}[/dim]   "
                    f"now: [bold]{ev.samples_after[:1] or ['?']}[/bold]"),
                Text.from_markup(
                    f"  {OK} healed via {ev.via}"
                    + (f" ({ev.model}, round {ev.rounds})" if ev.via == "llm" else "")
                    + " · lock updated · confirming over next 2 runs"),
            )
            err_console.print(Panel(body, border_style="green",
                                    title=f"[green]healed · {ev.field}[/green]",
                                    title_align="left"))
        elif ev.outcome == "absent":
            err_console.print(Panel(Text.from_markup(
                f"  field [bold]{ev.field}[/bold] appears REMOVED from the site.\n"
                f"  model note: {ev.note}\n"
                f"  This breaks the schema contract. To accept the new reality:\n"
                f"    [bold]stalin schema bump <source> --major[/bold]"),
                border_style="red", title=f"[red]contract breach · {ev.field}[/red]",
                title_align="left"))
        else:
            err_console.print(Panel(Text.from_markup(
                f"  {FAIL} could not heal [bold]{ev.field}[/bold]\n"
                f"  {ev.note or 'all rungs exhausted'}\n"
                f"  serving last-good data · try [bold]stalin heal --interactive[/bold] "
                f"or re-describe the field and [bold]stalin add --recompile[/bold]"),
                border_style="red", title=f"[red]broken · {ev.field}[/red]",
                title_align="left"))


def summary_line(res) -> None:
    """One line per source; the quiet-on-success contract."""
    healed = f"  ({len([e for e in res.heal_events if e.outcome=='healed'])} field healed)" \
        if any(e.outcome == "healed" for e in res.heal_events) else ""
    marks = {"ok": OK, "healed": OK, "stale": WARN, "blocked": FAIL, "broken": FAIL,
             "not-modified": OK}
    mark = marks.get(res.status, WARN)
    line = f"  {mark} [bold]{res.source}[/bold]  "
    if res.status in ("ok", "healed"):
        sv = res.envelope.get("schema_version", "?")
        line += (f"{len(res.items)} items  {res.fetch_elapsed:.1f}s  "
                 f"schema v{sv}{healed}")
    elif res.status == "stale":
        line += f"[yellow]STALE[/yellow] ({res.error}) — serving last-good {len(res.items)} items"
    else:
        line += f"[red]{res.status.upper()}[/red] — {res.error}"
    err_console.print(line)


def items_table(source: str, url: str, items: list, max_rows: int = 8) -> None:
    if not items:
        err_console.print("  [dim](no items)[/dim]")
        return
    cols = list(items[0].keys())
    t = Table(title=f"{source} · {url}", title_justify="left",
              caption=f"{len(items)} items" + (f" (showing {max_rows})" if len(items) > max_rows else ""))
    for c in cols:
        t.add_column(c, overflow="ellipsis", max_width=48)
    for row in items[:max_rows]:
        t.add_row(*[str(row.get(c, ""))[:80] if row.get(c) is not None else "[dim]—[/dim]"
                    for c in cols])
    err_console.print(t)


def emit_json(envelope: dict) -> None:
    print(json.dumps(envelope, ensure_ascii=False, indent=2))


def history_timeline(source: str, events: list[dict]) -> None:
    if not events:
        err_console.print(f"  [dim]no history for {source}[/dim]")
        return
    err_console.print(Rule(f"history · {source}", style="dim"))
    for ev in events:
        ts = ev.get("ts", "?")
        kind = ev.get("event", "?")
        if kind == "heal":
            err_console.print(
                f"  [dim]{ts}[/dim]  {OK} [bold]heal[/bold] {ev.get('field')}  "
                f"[red strike]{ev.get('old')}[/red strike] ──→ "
                f"[green]{ev.get('new')}[/green]  "
                f"[dim]via {ev.get('via')} · {', '.join(ev.get('cause', []))}[/dim]")
        elif kind in ("heal-failed", "thrash-revert"):
            err_console.print(f"  [dim]{ts}[/dim]  {FAIL} [bold]{kind}[/bold] "
                              f"{ev.get('field')}  [dim]{ev.get('note', '')}[/dim]")
        elif kind == "heal-confirmed":
            err_console.print(f"  [dim]{ts}[/dim]  {OK} [bold]confirmed[/bold] "
                              f"{ev.get('field')}")
        elif kind == "blocked":
            err_console.print(f"  [dim]{ts}[/dim]  {FAIL} [bold]blocked[/bold]  "
                              f"[dim]{ev.get('reason')}[/dim]")
        elif kind == "drift":
            err_console.print(f"  [dim]{ts}[/dim]  {WARN} [bold]drift[/bold] "
                              f"{ev.get('field')}  [dim]{', '.join(ev.get('cause', []))}[/dim]")
        elif kind == "compile":
            err_console.print(f"  [dim]{ts}[/dim]  {OK} [bold]compiled[/bold]  "
                              f"[dim]{ev.get('fields')} fields, model {ev.get('model')}[/dim]")
        else:
            err_console.print(f"  [dim]{ts}[/dim]  {kind}")
