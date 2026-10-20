from __future__ import annotations

import sqlite3

import pytest

from conftest import SAMPLES, compile_q
from datagen import generate, topological_order
from nl2sql.schema import load_database, quote_ident

ROWS = 500


@pytest.fixture(scope="module")
def scaled_university(tmp_path_factory):
    out = tmp_path_factory.mktemp("scaled") / "university.scaled.db"
    generate(SAMPLES / "university.db", out, ROWS, seed=7, verbose=False)
    return load_database(out)


@pytest.fixture(scope="module")
def scaled_retail(tmp_path_factory):
    out = tmp_path_factory.mktemp("scaled") / "retail.scaled.db"
    generate(SAMPLES / "retail.db", out, ROWS, seed=7, verbose=False)
    return load_database(out)


def test_tables_are_preserved(scaled_university, university):
    assert set(scaled_university.symbols.tables) == set(university.symbols.tables)


def test_row_counts_are_right(scaled_university):
    for table in scaled_university.symbols.tables.values():
        assert table.row_count == ROWS, table.name


def test_foreign_keys_hold(scaled_university):
    conn = scaled_university.connect()
    try:
        for name in scaled_university.symbols.tables:
            bad = conn.execute(
                f"PRAGMA foreign_key_check({quote_ident(name)})"
            ).fetchall()
            assert not bad, f"{name} has dangling foreign keys"
    finally:
        conn.close()


def test_not_null_is_respected(scaled_university):
    conn = scaled_university.connect()
    try:
        for table in scaled_university.symbols.tables.values():
            for col in table.columns.values():
                if col.nullable:
                    continue
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {quote_ident(table.name)} "
                    f"WHERE {quote_ident(col.name)} IS NULL"
                ).fetchone()[0]
                assert count == 0, col.qname
    finally:
        conn.close()


def test_primary_keys_are_unique(scaled_university):
    conn = scaled_university.connect()
    try:
        for table in scaled_university.symbols.tables.values():
            if not table.primary_key:
                continue
            keys = ", ".join(quote_ident(c) for c in table.primary_key)
            total, distinct = conn.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT {keys}) "
                f"FROM {quote_ident(table.name)}"
                if len(table.primary_key) == 1
                else f"SELECT COUNT(*), (SELECT COUNT(*) FROM "
                     f"(SELECT DISTINCT {keys} FROM {quote_ident(table.name)})) "
                     f"FROM {quote_ident(table.name)}"
            ).fetchone()
            assert total == distinct, table.name
    finally:
        conn.close()


def test_unique_columns_stay_unique(scaled_retail):
    conn = scaled_retail.connect()
    try:
        for table in scaled_retail.symbols.tables.values():
            for col in table.columns.values():
                if not col.unique or col.is_pk:
                    continue
                total, distinct = conn.execute(
                    f"SELECT COUNT({quote_ident(col.name)}), "
                    f"COUNT(DISTINCT {quote_ident(col.name)}) "
                    f"FROM {quote_ident(table.name)}"
                ).fetchone()
                assert total == distinct, col.qname
    finally:
        conn.close()


def test_topological_order_puts_parents_first(university):
    order, cycles = topological_order(university.symbols)
    assert not cycles
    position = {name: i for i, name in enumerate(order)}
    for table in university.symbols.tables.values():
        for fk in table.foreign_keys:
            if fk.to_table != table.name:
                assert position[fk.to_table] < position[table.name]


def test_generation_is_deterministic(tmp_path):
    first = tmp_path / "a.db"
    second = tmp_path / "b.db"
    generate(SAMPLES / "retail.db", first, 100, seed=3, verbose=False)
    generate(SAMPLES / "retail.db", second, 100, seed=3, verbose=False)
    assert first.read_bytes() == second.read_bytes()


# ------------------------------------------------ soundness on fresh data
DEMO = [
    "how many students are there",
    "show students with gpa above 9",
    "how many courses per department",
    "average gpa per department",
    "which departments have students with gpa over 9",
    "is there any student with gpa above 9.5",
    "top 5 students by gpa",
    "show courses with credits at least 4",
]


@pytest.mark.parametrize("question", DEMO)
def test_the_compiler_stays_sound_on_generated_data(scaled_university, question):
    result = compile_q(question, scaled_university)
    assert result.ok, question
    assert result.source == "compiler", question
    assert result.equivalence is not None and result.equivalence.passed, question


def test_fired_rules_still_have_passing_preconditions(scaled_university):
    for question in DEMO:
        result = compile_q(question, scaled_university)
        for entry in result.trace:
            assert all(c.passed for c in entry.checks), (question, entry.rule)
