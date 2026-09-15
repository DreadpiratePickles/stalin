<div align="center">

# ☭ stalin

**Turn any website into a self-healing, typed JSON API.**

*He seizes the means of extraction.*

[![python](https://img.shields.io/badge/python-3.11+-blue)](#install)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![llm](https://img.shields.io/badge/LLM-local%20via%20ollama-orange)](#the-llm-is-optional)
[![robots.txt](https://img.shields.io/badge/robots.txt-respected%20by%20default-brightgreen)](#politeness-doctrine)

</div>

---

Every scraper you have ever written is already dead. It just doesn't know it yet.
Some Tuesday, a frontend dev renames `.score` to `.karma`, your pipeline fills
with nulls, and you find out three days later when a dashboard flatlines.

**stalin does not tolerate this.** You describe *what* you want in plain
English, once. stalin compiles that into CSS selectors, watches them like a
paranoid quartermaster, and when the website changes its layout — *and it
will* — stalin detects the drift in microseconds, re-locates your data, proves
the fix against five deterministic gates, and keeps shipping typed JSON like
nothing happened. The selector was purged. The schema survives. The API
never changes shape without your explicit order.

Your scraper doesn't break. It gets *reeducated*.

## The 60-second demo

```console
$ pip install stalin-scraper
$ stalin init
$ stalin add https://news.ycombinator.com -n stories \
    --item "each story row on the front page" \
    -f "title:    str  the headline text of each story
        url:      url  the link the headline points to
        points:   int | null  the upvote count
        comments: int | null  number of comments"

  ✓ fetched news.ycombinator.com  200 · 41 KB · 0.3s
  ✓ title        .titleline > a         verified 30/30  via heuristic · 2 fallbacks
  ✓ url          .titleline a @href     verified 30/30  via heuristic · 2 fallbacks
  ✓ points       .score                 verified 29/30  via heuristic · 2 fallbacks
  ✓ comments     .subline a:last-child  verified 30/30  via llm · 2 fallbacks

  ✓ wrote sources/stories.yml, .stalin/lock.json (schema v1.0.0, 8 fallbacks)

$ stalin run stories | jq '.items[0]'
{ "title": "Show HN: …", "url": "https://…", "points": 312, "comments": 148 }

$ stalin serve
  stalin API → http://127.0.0.1:8411
    GET /v1/stories        typed JSON, schema v1.0.0
    GET /openapi.json      OpenAPI 3.1
    GET /healthz           per-source status
```

And then — weeks later, when the site ships a redesign — the moment this tool
exists for. This is a real transcript (demo site renamed every class and
swapped `<h2>` for `<div>`):

```console
$ stalin run stories

  ⚠ drift detected · stories.points
    selector  .score
    signals   zero-match

  ✓ heal title     .story .headline ──→ .story .hdr   via heuristic
  ✓ heal url       .headline a @href ──→ .hdr a @href  via heuristic
  ✓ heal points    .score ──→ .karma                   via heuristic
  ✓ heal comments  .comments ──→ .replies              via heuristic

  ✓ stories  20 items  schema v1.0.0  (4 fields healed)

$ stalin history stories
  11:16:16Z  ✓ heal points  .score ──→ .karma  via heuristic · zero-match
  11:16:28Z  ✓ confirmed points
```

Four fields, one redesign, zero human intervention, zero schema changes,
milliseconds of healing. The comrades downstream consuming `/v1/stories`
never noticed anything.

## Why this exists

| | |
|---|---|
| 🩹 **It never breaks silently** | Five drift signals run on every scrape — including the nasty case where your selector still matches *something*, just the wrong something. Detection costs microseconds, not LLM calls. |
| 📐 **The schema is law** | Fields are typed (`int`, `url`, `datetime`, `list[str]`, `enum[…]`), validated through pydantic on every run, and versioned with semver. Healing may change *where* data comes from — **never what shape it has**. Breaking changes require an explicit `stalin schema bump --major`. Yes, it makes you type `--major`. That's the point. |
| 🏠 **Local-first, zero API keys** | The healer runs on your machine through Ollama. No cloud, no per-token bill, no data leaving the building. |
| 🧠 **The LLM is optional** | Compile and healing run a ladder: stored fallbacks → DOM-statistics heuristics → local LLM. On most pages the heuristics do everything in milliseconds and the model is never consulted. No Ollama at all? You still get full detection + alerting. |

## Any website → a live, queryable API  ⚡ *new in 0.2*

A `static_feed` gives you one fixed page as JSON. But most of the web you'd
actually want as an API is **parameterized** — a search box, a profile page,
a lookup by ID. stalin turns those into live typed endpoints. Put a `{param}`
in the URL and give one example to compile against:

```console
$ stalin add "https://quotes.toscrape.com/tag/{tag}/" -n quotes \
    --item "each quote block on the page" \
    --example tag=love \
    -f "text:   str        the quote text itself
        author: str        who said it
        tags:   list[str]  the topic tags on the quote"

  ✓ fetched quotes.toscrape.com/tag/love/  200 · 12 KB
  ✓ text    .quote .text   verified 10/10   via heuristic
  ✓ author  .author        verified 10/10   via heuristic
  ✓ tags    .tags .tag      verified 10/10   via heuristic
  live lookup — params: tag
```

That website has **no API**. It does now:

```console
$ stalin run quotes --param tag=courage --json | jq '.count'
2

$ stalin serve
  GET /v1/quotes?tag=humor      # live, typed, on demand
  GET /openapi.json             # the param is a documented query parameter
```
```console
$ curl 'http://127.0.0.1:8411/v1/quotes?tag=love' | jq '.items[0].author'
"André Gide"
$ curl 'http://127.0.0.1:8411/v1/quotes'          # missing required param
{"error": "missing required param(s): tag", "params": {"tag": "str"}}
```

**And it becomes a typed tool for your agents.** Every parameterized source is
auto-exposed over MCP as `lookup_<name>(param=…)` with a generated input
schema — so Claude (or any agent) calls `lookup_quotes(tag="stoicism")` and
gets back schema-guaranteed JSON, no scraping code in the agent, no HTML in the
prompt. That's stalin's answer to *"why not just let the agent scrape?"* — the
tool's contract stays honest even as the site churns.

### Lookups self-heal too — the hard version

A live lookup returns different content for every query, so "zero results" can
mean *the query was empty* or *the site broke*. stalin distinguishes them: you
declare a `not_found` signal for legitimately-empty pages (never healed
against, same rule as block pages), and healing is anchored to a **heal
fixture** — the known-good example params you compiled with. When a real query
comes back unexpectedly empty, stalin re-verifies selectors against the fixture
(stable, known structure), heals there, and re-applies the fix to your query.
Per-query variation never gets mistaken for drift.

*Proven live:* pointed at a page, compiled, then renamed every CSS class and
swapped the tags — the very next `?param=…` request healed three fields against
the fixture and returned correct typed data, in one round, no human touch.

### What works today, honestly

`live_lookup` runs on static-HTML pages right now. **Path** params (`/{id}/`)
and **query** params (`?q=…`) both work. What it does *not* do yet, and won't
pretend to: JavaScript-rendered pages (the Playwright engine seam exists but
isn't built), pagination/infinite-scroll, and anything behind a login or
CAPTCHA — those stay refused, by design. It does **public, unauthenticated,
static surfaces**. That covers a huge amount of "this site should've had an
API" — and none of the stuff that gets you sued.

## How healing works

```
drift detected on field F
  ├─ Rung 1: stored fallbacks          compile-time alternates, ~ms, no LLM
  ├─ Rung 2: heuristic re-location     DOM statistics scored against the
  │                                    field's fingerprint, ~ms, no LLM
  ├─ Rung 3: LLM re-location           local model, sentinel text protocol —
  │                                    works with any Ollama model
  └─ Rung 4: broken                    serve last-good data, exit 3, tell you
```

The proposers differ per rung. **The judge never changes**: every candidate
selector faces five deterministic acceptance gates —

1. **Schema** — every extracted value must cast to the declared type
2. **Cardinality** — match counts must stay near the historical baseline
3. **Shape** — values must match the field's learned value-pattern (or migrate
   to a *consistent* new one, which is recorded)
4. **Anchor** — if an old known-good value still exists on the page, the new
   selector must capture it **exactly** (substring lookalikes are rejected)
5. **Disjointness & robustness** — no annexing another field's selector, no
   position-brittle `:nth-child(7)` nonsense, no auto-generated class hashes

The LLM proposes. The gates dispose. No model opinion is ever trusted about
its own output — every accepted heal was *executed against the real DOM* and
survived all five gates. Accepted heals are applied immediately (data keeps
flowing) but marked `healed-unconfirmed` until two clean runs promote them;
a re-drift inside that window reverts the heal and flags the field instead of
thrashing on A/B-tested sites.

Everything is recorded. `git diff .stalin/lock.json` shows every selector the
healer has ever touched, and `.stalin/history/*.jsonl` is an append-only,
line-per-event audit log. Rewriting history is for websites, not for your
data pipeline.

## Install

```bash
pip install stalin-scraper
```

Optional but recommended — a local model for the healing rung:

```bash
# any ollama model works; small ones are fine (the gates do the hard part)
ollama pull qwen3:1.7b
```

Then check your environment:

```bash
stalin doctor
```

## Usage

| Command | What it does |
|---|---|
| `stalin init` | Scaffold a project (`stalin.yml`, `sources/`, `.stalin/`) |
| `stalin add <url> -n NAME -f "…"` | Compile a new source: fetch → generate selectors → verify → save |
| `stalin add "<url/{param}>" --example param=v …` | Compile a **live_lookup**: any templated URL → a queryable API |
| `stalin run SOURCE --param k=v` | Run a live lookup for specific params |
| `stalin run SOURCE -w "f__op=v" --sort -f --fields a,b --limit N` | Filter/sort/select/paginate results |
| `GET /v1/SOURCE?f__gt=1&sort=-f&fields=a,b&limit=N&q=…` | Query over HTTP (see [Query it](#query-it--new-in-03)) |
| `stalin ask SOURCE "plain-English question"` | Let the local LLM compile a query for you |
| `stalin run [SOURCE…]` | Extract now. Auto-heals on drift. JSON to stdout when piped |
| `stalin run --no-heal` | Detect drift, report, exit 3 — never heal (CI mode) |
| `stalin heal [SOURCE[.FIELD]]` | Force a heal pass (includes the LLM rung) |
| `stalin serve [--refresh 15m]` | HTTP API over cached snapshots + OpenAPI 3.1 |
| `stalin watch` | Foreground scheduler: run each source on its `schedule:` |
| `stalin mcp` | MCP server on stdio — plug your sources into Claude/any agent |
| `stalin schema show/bump` | Print the JSON Schema; explicitly version-bump the contract |
| `stalin history SOURCE[.FIELD]` | The heal/drift/confirm timeline |
| `stalin snapshot SOURCE` | Refresh the reference HTML snapshot |
| `stalin doctor` | Environment + per-source health check |

**Exit codes are a contract** (cron/CI friendly): `0` ok · `1` config error ·
`2` fetch blocked · `3` drift unhealed (stale data served) · `4` schema
contract breach.

### Declaring a source

`stalin add` writes this file — or write it yourself and let stalin compile it:

```yaml
# sources/stories.yml — the INTENT. Hand-editable, never touched by the healer.
name: stories
url: https://news.ycombinator.com
schedule: 15m
item: each story row on the front page      # natural language!
fields:
  title:
    type: str
    desc: the headline text of each story
  points:
    type: int | null
    desc: the upvote count; job postings have none
  url:
    type: url
    desc: the link the headline points to
contract:
  version: 1.0.0
  min_items: 20        # fewer than this trips the drift alarm
```

The compiled selectors live in `.stalin/lock.json` — machine-owned, committed,
reviewed in PRs like a lockfile. You own the *what*; stalin owns the *how*.
This separation is the whole trick: the healer can rewrite selectors forever
without ever dirtying a file you edit.

### Types

`str` · `int` · `float` · `bool` · `url` (resolved absolute) · `datetime`
(ISO-8601 out) · `list[str]` · `enum[a,b,c]` — all nullable via `| null`.

### Query it  🔎 *new in 0.3*

Every source is now a **queryable** API — filter, sort, select fields, paginate,
and full-text search over the extracted items. Same grammar on HTTP, the MCP
tools, and the CLI. No more piping everything through `jq`.

```console
# 3 love quotes NOT by Marilyn Monroe, sorted by author, trimmed payload
$ curl "localhost:8411/v1/quotes?tag=love&author__ne=Marilyn%20Monroe&sort=author&fields=text,author&limit=3"
{
  "count": 7,          // total matching the filters (before limit)
  "returned": 3,       // how many came back
  "items": [ {"author": "André Gide",  "text": "…"}, … ],
  "_query": {"filters": {"author__ne": "Marilyn Monroe"},
             "sort": "author", "fields": ["text","author"], "limit": 3, "offset": 0}
}
```

**Controls:** `sort=-points,author` (`-` = desc) · `fields=a,b` (project) ·
`limit` / `offset` · `q=` (case-insensitive substring across text fields).

**Filters** are `field` (equality) or `field__op`:

| op | meaning | op | meaning |
|---|---|---|---|
| `__eq` `__ne` | equals / not | `__in` | `points__in=90,212` |
| `__gt` `__gte` `__lt` `__lte` | ordering (int/float/datetime) | `__contains` | substring, or list membership |
| `__isnull` | `points__isnull=true` | | |

Filters are **type-aware**: `points__gt=100` compares as an int, `when__gte=2026-01-01`
as a datetime. A bad value is a typed **400**, never a 500:

```json
{"error":"invalid_query","param":"points__gt","code":"invalid_value","expected_type":"int"}
```

**One rule where lookups and filters share a URL:** for a `live_lookup` source, a
bare key that matches a declared param (`?tag=love`) drives the fetch; everything
else is a control or a filter. If a field happens to share a param's name, filter
it explicitly with `field__eq=…`.

**From the terminal** — identical grammar:

```bash
stalin run quotes -p tag=love -w "author__ne=Marilyn Monroe"   --sort author --fields text,author --limit 3
```

**For agents (MCP):** `get_data` and every `lookup_<source>` tool take
`filter` (a `{field__op: value}` object), `sort`, `fields`, `limit`, `offset`,
and `q`. Each tool's description lists the source's filterable fields and types,
so a model can query precisely straight from `get_schema` — no scraping code,
no HTML in the prompt.

Self-healing is untouched: the query layer runs on already-typed items,
downstream of extraction and healing.

## Ask it in plain English  🗣️ *new in 0.4*

Don't want to learn the filter grammar? Ask. A local model compiles your
question into a query and runs it through the exact engine from
[Query it](#query-it--new-in-03) — same filters, same types, same safety.

```console
$ stalin ask quotes "3 love quotes not by Marilyn Monroe, just author and text"
  ✓ compiled → {'filters': {'author__ne': 'Marilyn Monroe'}, 'fields': ['author','text'], 'limit': 3}
  ✓ quotes  3/7 items
```
```
GET /v1/quotes/ask?q=love%20quotes%20over%20100%20points%2C%20newest%20first
```
And as an MCP tool for agents — every source gets `ask_<source>(question)`.

**The LLM only proposes; the engine disposes.** The model never sees the data
and never decides the answer — it emits a query in stalin's own grammar,
`parse_query` validates it against the schema (a bad guess is retried, not
served), and the deterministic engine runs it. Every response echoes the
compiled query in `_query` and the original in `_meta.question`, so you always
see exactly what it did — it can be incomplete, but never silently wrong.

Compilation quality tracks your local model: a small model (e.g. `qwen3:1.7b`)
handles clear questions well and occasionally drops a constraint; a larger one
is sharper. No Ollama, no `ask` — the structured query layer still works.

## The API

`stalin serve` gives you versioned, contract-stable endpoints over cached
snapshots (your consumers never wait on a fetch, and target sites never sit
in your request path):

```
GET  /v1/stories           → {source, fetched_at, schema_version, stale, count, items[]}
GET  /v1/stories/schema    → JSON Schema for the items
POST /v1/stories/refresh   → 202, background re-run (rate-limit guarded)
GET  /openapi.json         → OpenAPI 3.1 for everything
GET  /healthz              → per-source status: verified/healed/stale/broken
```

Healed-but-unconfirmed data is honestly labeled: `"_meta": {"healed": true,
"healed_fields": [...]}`. Stale last-good data says `"stale": true`. There is
no propaganda in the payload.

### MCP: feed your agents

```bash
stalin mcp    # stdio server: list_sources, get_data, get_schema
```

Point Claude Code (or any MCP client) at it and your agent gets typed,
self-healing website data as tools. Add to `.mcp.json`:

```json
{"mcpServers": {"stalin": {"command": "stalin", "args": ["mcp"]}}}
```

## The LLM is optional

No Ollama? stalin still compiles most pages (the heuristic engine reads DOM
statistics, not tea leaves) and still detects every drift — it just can't run
rung 3, so a heal that fallbacks + heuristics can't solve exits `3` and tells
you to fix it. With Ollama, any model works: the protocol is
plain text with a sentinel line, not tool-calling — a 1.7B model on a potato
laptop heals real drift in under a minute, because the gates do the thinking.

## Politeness doctrine

For a tool with this name, it is suspiciously well-behaved:

- **robots.txt respected by default.** Overriding is per-source, explicit, and
  nags you on every single run.
- **Rate-limited** (1 req / 2 s per host, jittered) with `Retry-After` honored
  and exponential backoff.
- **Honest User-Agent** — no browser impersonation in the default engine, ever.
- **The fetch classifier never heals against a block page.** A 403, a
  Cloudflare challenge, a login redirect — those are fetch problems, not drift.
  stalin serves last-good data and says so, instead of learning to extract
  garbage from an "Access Denied" page.
- No auth flows, no CAPTCHA anything, no paywalls. Hard line.

## vs. the alternatives

| | stalin | Firecrawl | Crawl4AI | hand-rolled BS4 |
|---|---|---|---|---|
| Survives site redesigns | **yes, automatically, verified** | no | no | you, at 2am |
| Typed schema contract | **semver'd, validated every run** | markdown out | markdown out | whatever you wrote |
| Detects silent breakage | **5 signals, every run** | – | – | – |
| Works fully offline/local | **yes (BYO Ollama or none)** | cloud credits | yes | yes |
| Serves an API + OpenAPI | **built in** | cloud | – | – |
| Parameterized lookup API (`?q=…`) | **built in** | – | – | you write it |
| MCP server | **built in** | cloud | yes | – |
| Cost per 10k pages | **$0** | ~$8–83 | $0 | your weekend |

Different tools for different jobs: Firecrawl/Crawl4AI turn pages into LLM-food
(markdown). stalin turns pages into **production data APIs with a stability
guarantee**. If your scraper feeds a dashboard, a pipeline, or a product —
that guarantee is the product.

## Architecture (for the curious)

```
stalin/
├── heuristics.py   candidate proposers: DOM statistics, no LLM (the workhorse)
├── gates.py        the five acceptance gates (the judge)
├── heal.py         the 4-rung ladder (the crown jewel)
├── drift.py        5 cheap signals, microseconds, every run
├── fingerprint.py  value shapes, node signatures, EMA baselines
├── compilepipe.py  `add`: NL descriptions → verified selectors
├── engine.py       fetch + politeness + the block-page classifier
├── extract.py      selector execution + typed casting
├── schema.py       the type system + pydantic + JSON Schema + semver
├── lockfile.py     .stalin/lock.json (machine-owned, git-committed)
├── runner.py       orchestration: fetch→classify→extract→drift→heal→emit
├── serve.py        hand-rolled ASGI app (uvicorn), OpenAPI 3.1
├── mcp_server.py   MCP over stdio
└── cli.py          typer + rich (the propaganda department)
```

Design lineage, honestly credited: the *proposers-vs-judge* split is borrowed
from [karpathy/llm-council](https://github.com/karpathy/llm-council) — many
cheap opinions, one deterministic verdict — and the sentinel text protocol
(instead of brittle structured tool-calls) is why a 1.7B local model is
enough. Small models can't fill out forms reliably, but they can end a
sentence with `SELECTOR: span.karma`.

## Roadmap

- [x] **Live parameterized lookups** (`{param}` URLs → `?q=…` APIs) — *0.2*
- [x] **Parameterized MCP tools** (`lookup_<source>(param=…)`) — *0.2*
- [x] **Queryable results** — filter/sort/fields/paginate/search on every surface — *0.3*
- [x] **`ask_<source>("plain-English question")`** — NL → the query engine, on every surface — *0.4*
- [ ] `change_watch` mode — poll a page, emit a typed webhook on change
- [ ] `aggregate` mode — one schema joined across N sites (entity resolution)
- [ ] `batch` mode — POST many inputs, get a typed array back
- [ ] JS rendering engine plugin (playwright/scrapling — the seam already exists)
- [ ] Pagination (`next:` selector + `max_pages`)
- [ ] Item-selector healing (fields heal; the container selector doesn't yet)

## Contributing

Issues and PRs welcome. The bar for a heal-logic change: it must keep every
gate deterministic. The bar for a new engine: implement `fetch()`, nothing
else. The bar for README jokes: they must be about scrapers, not history.

## License

MIT. Free as in "the collective owns the means of extraction."

---

<div align="center">
<i>Built local-first. No cloud was consulted. The selectors were purged;
the schema endures.</i>
</div>
