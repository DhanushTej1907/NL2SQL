"""An unseen schema: PascalCase names, ``<Thing>Id`` keys, real-world data.

Chinook is a smoke test, not part of the graded demo. It is skipped when
the file is not present.
"""

from __future__ import annotations

import pytest

from conftest import SAMPLES, compile_q

CHINOOK = SAMPLES / "chinook.db"

pytestmark = pytest.mark.skipif(
    not CHINOOK.exists(), reason="samples/chinook.db is not present"
)


@pytest.fixture(scope="module")
def chinook():
    from nl2sql.schema import load_database

    return load_database(CHINOOK)


def test_the_schema_is_introspected(chinook):
    assert len(chinook.symbols.tables) > 5
    assert any(t.primary_key for t in chinook.symbols.tables.values())
    assert any(e.status == "declared" for e in chinook.symbols.edges())


def test_pascal_case_names_are_split(chinook):
    from nl2sql.tagger import split_identifier

    multiword = [
        c.name for c in chinook.symbols.all_columns()
        if len(split_identifier(c.name)) > 1
    ]
    assert multiword, "this schema should contain multi-word identifiers"


QUESTIONS = [
    "how many tracks are there",
    "which artists have albums",
    "is there any track with milliseconds above 5000000",
    "top 5 tracks by milliseconds",
]


@pytest.mark.parametrize("question", QUESTIONS)
def test_questions_compile_and_stay_equivalent(chinook, question):
    result = compile_q(question, chinook)
    assert result.ok, question
    assert result.source == "compiler", question
    assert result.equivalence is not None and result.equivalence.passed, question


def test_rules_fire_on_an_unseen_schema(chinook):
    fired: set[str] = set()
    for question in QUESTIONS:
        fired.update(r.split()[0] for r in compile_q(question, chinook).fired_rules())
    assert "R1" in fired and "R3" in fired
