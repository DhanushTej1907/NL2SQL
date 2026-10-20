"""Loading a MySQL-style dump, and a schema with no declared keys."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import SAMPLES, compile_q
from nl2sql.schema import load_database

DUMP = SAMPLES / "retail_dump.sql"


@pytest.fixture(scope="module")
def dumped():
    db = load_database(DUMP)
    yield db
    db.cleanup()


# ------------------------------------------------------------- the dump
def test_the_dump_loads_without_fatal_errors(dumped):
    assert dumped.symbols.tables
    assert dumped.origin.suffix == ".sql"
    assert dumped.path.suffix == ".db"


def test_the_dump_has_the_same_tables_as_the_sqlite_sample(dumped, retail):
    assert set(dumped.symbols.tables) == set(retail.symbols.tables)


def test_the_dump_has_the_same_row_counts(dumped, retail):
    for name, table in retail.symbols.tables.items():
        assert dumped.symbols.tables[name].row_count == table.row_count


def test_declared_foreign_keys_survive_the_transpile(dumped, retail):
    def edges(db):
        return {
            (e.from_table, tuple(e.from_columns), e.to_table)
            for e in db.symbols.edges()
            if e.status != "inferred"
        }

    assert edges(dumped) == edges(retail)
    assert all(e.trusted for e in dumped.symbols.edges())


def test_primary_keys_survive_the_transpile(dumped, retail):
    for name, table in retail.symbols.tables.items():
        assert dumped.symbols.tables[name].primary_key == table.primary_key


def test_the_same_question_gives_the_same_answer_on_both(dumped, retail):
    question = "show the name and price of products in Electronics"
    from_dump = compile_q(question, dumped)
    from_db = compile_q(question, retail)
    assert from_dump.ok and from_db.ok
    assert sorted(from_dump.rows) == sorted(from_db.rows)


def test_rules_still_fire_on_the_dumped_database(dumped):
    result = compile_q("which customers have orders with total above 20000", dumped)
    assert any(r.startswith("R2") for r in result.fired_rules())
    assert result.equivalence.passed


def test_a_broken_statement_becomes_a_diagnostic_not_a_crash(tmp_path):
    path = tmp_path / "messy.sql"
    path.write_text(
        "CREATE TABLE `zorb` (`id` int(11) NOT NULL, PRIMARY KEY (`id`)) "
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;\n"
        "INSERT INTO `zorb` VALUES (1),(2);\n"
        "INSERT INTO `nowhere` VALUES (1);\n",
        encoding="utf-8",
    )
    db = load_database(path)
    try:
        assert "zorb" in db.symbols.tables
        assert db.symbols.tables["zorb"].row_count == 2
        assert db.load_diagnostics, "the bad statement should be reported"
    finally:
        db.cleanup()


# ------------------------------------------------------------- nokeys
def test_joins_still_work_through_inferred_edges(nokeys):
    result = compile_q("which teams have players with score above 90", nokeys)
    assert result.ok and result.source == "compiler"
    assert result.rows


def test_r1_fires_without_any_declared_constraint(nokeys):
    result = compile_q("is there any player with score above 95", nokeys)
    assert any(r.startswith("R1") for r in result.fired_rules())


def test_r2_and_r4_are_skipped_with_a_stated_reason(nokeys):
    result = compile_q("which teams have players with score above 90", nokeys)
    fired = result.fired_rules()
    assert not any(r.startswith("R2") for r in fired)
    assert not any(r.startswith("R4") for r in fired)
    reasons = " ".join(e.reason() for e in result.skipped_rules)
    assert "foreign key" in reasons or "primary key" in reasons


def test_the_duplicate_row_is_preserved(nokeys):
    conn = nokeys.connect()
    try:
        rows = conn.execute(
            'SELECT COUNT(*) FROM (SELECT "id", "name", "team_id", "score", '
            'COUNT(*) AS c FROM "player" GROUP BY 1,2,3,4 HAVING c > 1)'
        ).fetchone()
        assert rows[0] >= 1
    finally:
        conn.close()


def test_equivalence_still_holds_on_nokeys(nokeys):
    for question in (
        "how many players are there",
        "which teams have players with score above 90",
        "is there any player with score above 95",
        "show players with score above 50",
    ):
        result = compile_q(question, nokeys)
        assert result.ok, question
        assert result.equivalence.passed, question
