#!/usr/bin/env python3
"""Generate the 'lobsterwire' demo site — a fake HN-style front page.

    python3 demo/site.py v1 > /tmp/lobsterwire/index.html   # original design
    python3 demo/site.py v2 > /tmp/lobsterwire/index.html   # THE REDESIGN

v2 keeps identical data but renames every class and swaps <h2> for <div> —
exactly the kind of Tuesday deploy that kills ordinary scrapers.
"""
import random
import re
import sys

TITLES = [
    "Quantum cache invalidation, solved", "Show HN: I taught a flatworm SQL",
    "The case against microservices", "Rust in the Linux kernel: year five",
    "Why your scraper broke last Tuesday", "Postgres as a message queue, revisited",
    "A tiny language model that heals selectors", "SQLite is all you need",
    "The economics of GitHub stars", "Self-hosting email in 2026: still no",
    "Ollama on a potato: benchmarks", "The last CSS selector you'll ever write",
    "Typed JSON or it didn't happen", "Robots.txt is a social contract",
    "ETags: the API you already have", "Drift detection with boring statistics",
    "Local-first is eating the cloud", "Semver for scraped data",
    "MCP: the USB-C of AI tools", "The 15-second README",
]


def build() -> str:
    random.seed(7)
    rows = []
    for i, t in enumerate(TITLES, 1):
        pts, cm = random.randint(12, 480), random.randint(0, 260)
        rows.append(f"""    <article class="story">
      <h2 class="headline"><a href="/item/{i}">{t}</a></h2>
      <div class="meta"><span class="score">{pts} points</span> ·
        <a class="comments" href="/item/{i}#c">{cm} comments</a></div>
    </article>""")
    return ("<html><head><title>lobsterwire</title></head><body>\n"
            "<main class='feed'>\n" + "\n".join(rows) + "\n</main></body></html>")


def redesign(html: str) -> str:
    html = html.replace('<h2 class="headline">', '<div class="hdr">').replace("</h2>", "</div>")
    html = html.replace('class="score"', 'class="karma"')
    html = re.sub(r"(\d+) points", r"\1 karma", html)
    html = html.replace('class="comments"', 'class="replies"')
    html = re.sub(r"(\d+) comments", r"\1 replies", html)
    return html


if __name__ == "__main__":
    version = sys.argv[1] if len(sys.argv) > 1 else "v1"
    html = build()
    print(redesign(html) if version == "v2" else html)
