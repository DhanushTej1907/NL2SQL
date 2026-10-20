"""Every demo question must behave as ``demo_questions.md`` says it will.

The LLM questions run with the fallback switched off (the session-wide
default in ``conftest``), so this suite never touches the network: they are
expected to report ``fallback_unavailable`` cleanly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import ROOT, SAMPLES, compile_q

sys.path.insert(0, str(ROOT / "scripts"))

from demo_check import check, parse_questions  # noqa: E402

ITEMS = parse_questions(ROOT / "demo_questions.md")
COMPILER = [i for i in ITEMS if i.section == "compiler"]
LLM = [i for i in ITEMS if i.section == "llm"]

_loaded: dict[str, object] = {}


def _db(name: str):
    from nl2sql.schema import load_database

    if name not in _loaded:
        path = SAMPLES / name
        if not path.exists():
            pytest.skip(f"{name} is not present")
        _loaded[name] = load_database(path)
    return _loaded[name]


def test_the_file_parses():
    assert ITEMS, "demo_questions.md yielded no questions"


def test_there_are_enough_questions_per_database():
    counts: dict[str, int] = {}
    for item in COMPILER:
        counts[item.database] = counts.get(item.database, 0) + 1
    for name in ("university.db", "retail.db"):
        assert counts.get(name, 0) >= 8, f"{name} needs at least 8 compiler questions"


def test_there_are_llm_questions_per_database():
    counts: dict[str, int] = {}
    for item in LLM:
        counts[item.database] = counts.get(item.database, 0) + 1
    for name in ("university.db", "retail.db"):
        assert counts.get(name, 0) >= 2, f"{name} needs 2 fallback questions"


def test_every_rule_is_annotated_somewhere():
    annotated = {r for item in ITEMS for r in item.expected_rules}
    assert {"R1", "R2", "R3", "R4"} <= annotated


def test_each_rule_is_exercised_on_each_keyed_database():
    for name in ("university.db", "retail.db"):
        rules = {
            r
            for item in COMPILER
            if item.database == name
            for r in item.expected_rules
        }
        assert {"R1", "R2", "R3"} <= rules, (name, rules)


@pytest.mark.parametrize(
    "item", COMPILER, ids=lambda i: f"{i.database}:{i.question[:45]}"
)
def test_compiler_question(item):
    passed, detail = check(item, _db(item.database))
    assert passed, detail


@pytest.mark.parametrize(
    "item", LLM, ids=lambda i: f"{i.database}:{i.question[:45]}"
)
def test_llm_question_degrades_cleanly(item):
    """With the fallback disabled, these must still be handled, not crash."""
    passed, detail = check(item, _db(item.database))
    assert passed, detail
