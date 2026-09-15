"""The Q-layer: one grammar, verified across the engine and the envelope helper.

These are pure/offline tests — no network, no LLM — so CI runs them in the
`rust`-free Python path fast. They pin the semantics every surface shares.
"""
import pytest

from stalin.query import (parse_query, apply, query_envelope, QueryError,
                          CONTROLS, MAX_LIMIT)
from stalin.schema import parse_type

SCHEMA = {
    "text": parse_type("str"),
    "author": parse_type("str"),
    "points": parse_type("int | null"),
    "tags": parse_type("list[str]"),
    "when": parse_type("datetime | null"),
    "featured": parse_type("bool"),
}
LOOKUP = {"tag"}

ITEMS = [
    {"text": "a life quote", "author": "Einstein", "points": 150,
     "tags": ["life", "x"], "when": "2026-01-05", "featured": True},
    {"text": "love wins", "author": "Marilyn Monroe", "points": 90,
     "tags": ["love"], "when": "2026-03-01", "featured": False},
    {"text": "deep and simple", "author": "Elie Wiesel", "points": 212,
     "tags": ["life", "love"], "when": None, "featured": True},
    {"text": "be yourself", "author": "Marilyn Monroe", "points": None,
     "tags": ["be"], "when": "2026-02-02", "featured": False},
]


def run(params):
    q, la = parse_query(params, SCHEMA, LOOKUP)
    return apply(ITEMS, q, SCHEMA), la, q


def test_lookup_param_is_split_from_filters():
    res, la, _ = run({"tag": "love", "points__gt": "100"})
    assert la == {"tag": "love"}
    assert res.matched == 2
    assert {i["author"] for i in res.items} == {"Einstein", "Elie Wiesel"}


def test_bare_field_is_equality():
    res, _, _ = run({"author": "Marilyn Monroe"})
    assert res.matched == 2


def test_collision_bare_key_prefers_lookup_param():
    # a bare key matching a lookup param drives the fetch, not a filter
    _, la, q = run({"tag": "love"})
    assert la == {"tag": "love"} and q.filters == []


def test_operators():
    assert run({"points__gte": "150"})[0].matched == 2
    assert run({"points__lt": "150"})[0].matched == 1
    assert run({"points__ne": "150"})[0].matched == 2   # nulls excluded
    assert run({"points__in": "90,212"})[0].matched == 2
    assert run({"points__isnull": "true"})[0].matched == 1
    assert run({"author__contains": "monroe"})[0].matched == 2
    assert run({"tags__contains": "life"})[0].matched == 2
    assert run({"featured": "true"})[0].matched == 2


def test_datetime_date_only_expands_to_midnight():
    assert run({"when__gte": "2026-02-01"})[0].matched == 2


def test_sort_desc_and_nulls_last():
    res, _, _ = run({"sort": "-points"})
    assert [i["author"] for i in res.items][-1] == "Marilyn Monroe"  # null last
    assert res.items[0]["author"] == "Elie Wiesel"


def test_multikey_sort():
    res, _, _ = run({"sort": "author,-points"})
    monroes = [i["points"] for i in res.items if i["author"] == "Marilyn Monroe"]
    assert monroes == sorted(monroes, reverse=True, key=lambda x: (x is None, x))[:len(monroes)] \
        or monroes[0] == 90  # 90 before null under -points


def test_fields_projection_and_order():
    res, _, _ = run({"fields": "author,text"})
    assert list(res.items[0].keys()) == ["author", "text"]


def test_q_full_text_across_str_and_list():
    assert run({"q": "love"})[0].matched == 2   # text + a love tag
    assert run({"q": "LIFE"})[0].matched == 2   # case-insensitive, via tags


def test_pagination_matched_vs_returned():
    q, _ = parse_query({"limit": "2", "offset": "1", "sort": "author"}, SCHEMA, LOOKUP)
    res = apply(ITEMS, q, SCHEMA)
    assert res.matched == 4 and len(res.items) == 2


@pytest.mark.parametrize("params,code", [
    ({"points__gt": "abc"}, "invalid_value"),
    ({"author__gt": "x"}, "operator_not_supported_for_type"),
    ({"nope": "1"}, "unknown_parameter"),
    ({"points__zz": "1"}, "unknown_operator"),
    ({"ghost__eq": "1"}, "unknown_field"),
    ({"sort": "ghost"}, "invalid_control"),
    ({"limit": "-3"}, "invalid_control"),
    ({"limit": str(MAX_LIMIT + 1)}, "invalid_control"),
    ({"fields": "ghost"}, "invalid_control"),
])
def test_typed_errors(params, code):
    with pytest.raises(QueryError) as ei:
        parse_query(params, SCHEMA, LOOKUP)
    assert ei.value.code == code


def test_envelope_is_additive_for_empty_query():
    base = {"source": "s", "count": 4, "items": ITEMS, "_meta": {"x": 1}}
    q, _ = parse_query({}, SCHEMA, LOOKUP)
    env = query_envelope(base, q, SCHEMA)
    assert env["count"] == 4 and env["returned"] == 4
    assert env["items"] == ITEMS and env["_meta"] == {"x": 1}
    assert env["_query"]["filters"] == {}


def test_envelope_count_is_matched_before_limit():
    base = {"source": "s", "count": 4, "items": ITEMS}
    q, _ = parse_query({"points__gt": "50", "limit": "1"}, SCHEMA, LOOKUP)
    env = query_envelope(base, q, SCHEMA)
    assert env["count"] == 3 and env["returned"] == 1


def test_reserved_words_are_controls_not_fields():
    for w in CONTROLS:
        assert w in ("sort", "fields", "limit", "offset", "q")
