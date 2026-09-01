#!/usr/bin/env bash
# The full heal demo, start to finish. Run from anywhere; needs stalin installed.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
SITE="$WORK/site"
PROJ="$WORK/proj"
mkdir -p "$SITE" "$PROJ"

python3 "$HERE/site.py" v1 > "$SITE/index.html"
python3 -m http.server 8931 --directory "$SITE" >/dev/null 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT
sleep 1

cd "$PROJ"
stalin init
echo
echo "══ 1. compile the source ══════════════════════════════════════"
stalin add http://127.0.0.1:8931/ -n stories \
  --item "each story article in the feed" \
  -f "title: str the headline text of each story
url: url the link the headline points to
points: int the upvote count
comments: int number of comments"

echo
echo "══ 2. THE REDESIGN (every class renamed, h2 -> div) ═══════════"
python3 "$HERE/site.py" v2 > "$SITE/index.html"

echo
echo "══ 3. stalin runs, notices, heals, keeps shipping ═════════════"
stalin run stories

echo
echo "══ 4. the receipts ════════════════════════════════════════════"
stalin history stories
