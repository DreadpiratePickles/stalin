"""`ask` — compile a plain-English question into a structured query.

The LLM never touches the data and never decides the answer. It only proposes a
query in stalin's own grammar; `parse_query` validates it against the schema
(typed errors feed a short retry loop) and the deterministic engine runs it.
So `ask` is exactly `query` with an English front-end — no new semantics, and
the compiled query is echoed back so a human or agent can audit it.

Line protocol (reliable for small local models — reasoning, then a marker):

    QUERY:
    tag=love
    author__ne=Marilyn Monroe
    sort=author
    limit=3

Each line after QUERY: is `key=value` (value may contain spaces). Same spelling
as the HTTP query string and the CLI -w/-p flags.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Optional

from .config import SourceSpec
from .llm import LLMUnavailable, OllamaClient, complete
from .query import CONTROLS, OPERATORS, QueryError, parse_query

_MARKER = re.compile(r"QUERY:\s*(.*)$", re.IGNORECASE | re.DOTALL)


def _system(spec: SourceSpec) -> str:
    fields = "; ".join(f"{n} ({ft.spec})" for n, ft in spec.ftypes.items())
    lookup = (", ".join(spec.params) if spec.params else "(none)")
    return f"""You turn a plain-English question into a data query. Reply with a \
short line of reasoning, then a final block that starts with QUERY: and lists \
one key=value per line.

Dataset fields: {fields}
Lookup params (identify the page to fetch, set as bare name=value): {lookup}

How to write the query lines:
- Filter a field: field__OP=value, where OP is one of \
{', '.join(OPERATORS)}. Bare field=value means equals.
  (__gt/__gte/__lt/__lte only on int/float/datetime; __contains on text/list.)
- Controls: sort=-field (minus = descending), fields=a,b (only these), \
limit=N, offset=N, q=word (full-text substring across text fields).
- Only use field names from the dataset. Put the lookup param(s) if the question \
implies which page.
- Include EVERY constraint the question states: who/what to exclude (__ne), how \
many (limit), which fields (fields=), and any ordering (sort=). Do not drop one.
- Omit anything the question does not ask for. Values may contain spaces.

Example question: "the 3 highest-scored love quotes not by Marilyn Monroe, \
just author and text"
Example answer:
Filtering out Marilyn, sorting by score descending, limiting to 3.
QUERY:
tag=love
author__ne=Marilyn Monroe
sort=-points
fields=author,text
limit=3"""


def parse_query_block(text: str) -> dict:
    """Pull the key=value lines out of the model's QUERY: block, leniently."""
    m = _MARKER.search(text)
    body = m.group(1) if m else text
    params: dict = {}
    # Small models format the block as newline-, semicolon- or &-separated
    # key=value pairs (sometimes all on one line). Split on any of them; values
    # may still contain spaces.
    for tok in re.split(r"[;&\n]+", body):
        tok = tok.strip().lstrip("-*• ").strip()
        if not tok or "=" not in tok:
            continue
        key, _, value = tok.partition("=")
        key = key.strip().strip("`\"' ")
        value = value.strip().strip("`\"'")
        if not key or " " in key:      # a spaced key is prose, not a param
            continue
        params[key] = value
    return params


@dataclass
class AskResult:
    params: dict
    rounds: int = 0
    error: Optional[str] = None
    reasoning: str = ""


def compile_question(client: OllamaClient, spec: SourceSpec, question: str,
                     max_rounds: int = 3) -> AskResult:
    """English -> a validated flat param map. Retries on typed QueryError."""
    schema = spec.ftypes
    lookup_params = set(spec.params)
    feedback = ""
    last_params: dict = {}
    for rnd in range(1, max_rounds + 1):
        user = f"Question: {question}{feedback}"
        try:
            reply = complete(client, _system(spec), user)
        except LLMUnavailable as e:
            return AskResult(params=last_params, rounds=rnd, error=str(e))
        params = parse_query_block(reply)
        last_params = params
        try:
            _, lookup_args = parse_query(params, schema, lookup_params)
        except QueryError as qe:
            feedback = (f"\n\nYour previous QUERY had an error on '{qe.param}': "
                        f"{qe.detail or qe.code}. Fix only that and reply again "
                        f"with a corrected QUERY: block.")
            continue
        # degenerate compiles (empty, or missing a required lookup param) get one
        # more shot with a targeted nudge — small models occasionally whiff.
        missing_lookup = [pn for pn in lookup_params
                          if pn not in lookup_args
                          and (spec.params[pn].required and spec.params[pn].default is None)]
        if (not params or missing_lookup) and rnd < max_rounds:
            need = (f"include the required page param(s) {', '.join(missing_lookup)} and "
                    if missing_lookup else "")
            feedback = (f"\n\nYour QUERY was empty or incomplete. {need}add every "
                        f"filter/sort/limit the question implies, then reply with a "
                        f"QUERY: block.")
            continue
        return AskResult(params=params, rounds=rnd,
                         reasoning=reply.split("QUERY:")[0].strip()[:400])
    return AskResult(params=last_params, rounds=max_rounds,
                     error=f"could not compile a valid query in {max_rounds} rounds")


# --- execution across surfaces -------------------------------------------

async def run_ask(project, spec: SourceSpec, question: str,
                  client: Optional[OllamaClient] = None) -> dict:
    """Compile the question, run it through the query engine, return an envelope.

    Works for both static_feed (query the cached snapshot) and live_lookup
    (the compiled query may set the lookup param, driving a live fetch).
    """
    import asyncio
    import json as _json
    from pathlib import Path

    from .llm import LLMUnavailable, OllamaClient as _OC
    from .lockfile import Lock
    from .query import parse_query, query_envelope, QueryError

    if client is None:
        try:
            client = _OC.create(project.config.llm)
        except LLMUnavailable as e:
            return {"error": "llm_unavailable", "detail": str(e),
                    "question": question}

    ask = await asyncio.to_thread(compile_question, client, spec, question)
    if ask.error and not ask.params:
        return {"error": "ask_failed", "detail": ask.error, "question": question}

    try:
        query, lookup_args = parse_query(ask.params, spec.ftypes,
                                         set(spec.params))
    except QueryError as qe:
        return {**qe.as_dict(), "question": question, "compiled": ask.params}

    if spec.is_lookup:
        missing = spec.missing_params(lookup_args)
        if missing:
            return {"error": "missing_param", "question": question,
                    "compiled": ask.params,
                    "detail": f"the question did not imply required param(s): "
                              f"{', '.join(missing)}"}
        from .lookup import run_lookup
        lock = Lock.load(project.lock_path)
        lres = await run_lookup(project, spec, lock, lookup_args)
        base = lres.envelope or {"items": [], "count": 0}
    else:
        dp = project.data_path(spec.name)
        if not dp.exists():
            return {"error": "no_data", "question": question,
                    "detail": f"run `stalin run {spec.name}` first"}
        base = _json.loads(dp.read_text())

    env = query_envelope(base, query, spec.ftypes)
    env["_meta"] = {**env.get("_meta", {}), "question": question,
                    "compiled_rounds": ask.rounds}
    return env
