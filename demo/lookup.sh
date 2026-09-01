#!/usr/bin/env bash
# Turn a website with NO API into a live, queryable, self-healing JSON API.
# Target: quotes.toscrape.com — static, scraping-friendly, no official API.
set -euo pipefail
WORK="$(mktemp -d)"; cd "$WORK"
stalin init

echo; echo "══ compile a LIVE LOOKUP from a {templated} URL ═══════════════"
stalin add "https://quotes.toscrape.com/tag/{tag}/" -n quotes \
  --item "each quote block on the page" \
  --example tag=love \
  -f "text:   str        the quote text itself
      author: str        who said it
      tags:   list[str]  the topic tags on the quote"

echo; echo "══ now it's an API. query ANY tag, live, typed ════════════════"
stalin run quotes --param tag=humor  --json | jq '{count, first: .items[0].author}'
stalin run quotes --param tag=life   --json | jq '{count, first: .items[0].author}'

echo; echo "══ serve it over HTTP (+ OpenAPI, + as an MCP tool) ═══════════"
echo "  stalin serve   →   GET /v1/quotes?tag=courage"
echo "                     lookup_quotes(tag=...) as an MCP tool for agents"
