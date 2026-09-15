"""`ask`: NL → the query engine. Parsing + compile loop, fully offline.

The LLM is mocked so these run in CI with no Ollama. They pin the two things
that are ours (not the model's): the tolerant block parser, and that a compiled
query is always validated through `parse_query` before it runs.
"""
import pytest

from stalin.ask import parse_query_block, compile_question
from stalin.config import SourceSpec, FieldSpec, ParamSpec

WANT = {"tag": "love", "author__ne": "Marilyn Monroe",
        "fields": "author,text", "limit": "3"}


@pytest.mark.parametrize("text", [
    "reasoning here\nQUERY:\ntag=love\nauthor__ne=Marilyn Monroe\nfields=author,text\nlimit=3",
    "QUERY: tag=love; author__ne=Marilyn Monroe; fields=author,text; limit=3",
    "QUERY:\ntag=love&author__ne=Marilyn Monroe&fields=author,text&limit=3",
    "Sure!\nQUERY:\n- tag=love\n* author__ne=Marilyn Monroe\nfields=author,text\nlimit=3\nDone.",
])
def test_parser_shapes_all_equal(text):
    assert parse_query_block(text) == WANT


def test_parser_ignores_prose_and_backticks():
    assert parse_query_block("QUERY:\n`points__gt`=100\nsort=-points\nhope that helps") \
        == {"points__gt": "100", "sort": "-points"}


class FakeClient:
    """Returns canned replies in sequence; records the prompts it saw."""
    model = "fake"

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append(messages[-1]["content"])
        return {"content": self._replies.pop(0)}


def _spec():
    return SourceSpec(
        name="quotes", url="https://x/tag/{tag}/", mode="live_lookup",
        params={"tag": ParamSpec()},
        fields={"text": FieldSpec(type="str"), "author": FieldSpec(type="str"),
                "points": FieldSpec(type="int | null")})


def test_compile_happy_path():
    c = FakeClient(["ok\nQUERY:\ntag=love\nauthor__ne=Marilyn Monroe\nlimit=3"])
    r = compile_question(c, _spec(), "love quotes not by Marilyn, 3 of them")
    assert r.error is None
    assert r.params == {"tag": "love", "author__ne": "Marilyn Monroe", "limit": "3"}
    assert r.rounds == 1


def test_compile_retries_on_invalid_query():
    # first reply filters a non-existent field -> QueryError -> retry -> valid
    c = FakeClient([
        "QUERY:\ntag=love\nprice__gt=10",              # 'price' not a field
        "QUERY:\ntag=love\npoints__gt=10",             # fixed
    ])
    r = compile_question(c, _spec(), "love quotes over 10 points")
    assert r.error is None and r.rounds == 2
    assert r.params == {"tag": "love", "points__gt": "10"}
    assert "price" in c.calls[1]     # the error was fed back


def test_compile_retries_on_missing_lookup_param():
    c = FakeClient([
        "QUERY:\nauthor__ne=Marilyn Monroe",           # forgot the required tag
        "QUERY:\ntag=love\nauthor__ne=Marilyn Monroe",
    ])
    r = compile_question(c, _spec(), "love quotes not by Marilyn")
    assert r.rounds == 2 and r.params.get("tag") == "love"
