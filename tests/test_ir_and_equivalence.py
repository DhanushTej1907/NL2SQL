from __future__ import annotations

import sqlite3

import pytest
import sqlglot

from conftest import compile_q
from nl2sql import codegen, equivalence, ir
from nl2sql.ir import (
    AggCall,
    ColRef,
    Cmp,
    Limit,
    Literal,
    Project,
    Scan,
    Select,
    Sort,
    conjoin,
    conjuncts,
    walk_nodes,
)


# ----------------------------------------------------------------- IR
def test_nodes_are_immutable():
    node = Scan("widget")
    with pytest.raises(Exception):
        node.table = "other"


def test_pretty_printer_shows_the_tree(university):
    result = compile_q("which departments have students with gpa over 9.9", university)
    text = result.plan.pretty()
    assert "Project" in text and "Join" in text
    assert "intents" in text
    optimized = result.optimized_plan.pretty()
    assert "SemiJoin" in optimized


def test_intents_reach_the_plan(university):
    assert "existence" in compile_q(
        "is there any student with gpa above 9.9", university
    ).plan.intents
    assert "top_k" in compile_q("top 5 students by gpa", university).plan.intents
    assert "semi_join" in compile_q(
        "which departments have students with gpa over 9.9", university
    ).plan.intents


def test_join_nodes_carry_their_edge(university):
    result = compile_q("how many courses per department", university)
    joins = [n for n in walk_nodes(result.plan.root) if isinstance(n, ir.Join)]
    assert joins and joins[0].edge is not None
    assert joins[0].edge.status == "declared"


def test_conjunction_helpers():
    a = Cmp(">", ColRef("widget", "weight"), Literal(1))
    b = Cmp("<", ColRef("widget", "weight"), Literal(9))
    assert conjoin([]) is None
    assert conjoin([a]) is a
    assert len(conjuncts(conjoin([a, b]))) == 2


def test_walk_visits_every_node():
    tree = Limit(Sort(Project(Select(Scan("widget"), Literal(1)), ()), ()), 5)
    kinds = {type(n).__name__ for n in walk_nodes(tree)}
    assert kinds == {"Limit", "Sort", "Project", "Select", "Scan"}


# -------------------------------------------------------------- codegen
def test_identifiers_are_always_quoted(university):
    sql = compile_q("show students with gpa above 9.9", university).optimized_sql
    assert '"' in sql


def test_generated_sql_parses_as_one_select(university):
    for question in ("show students with gpa above 9.9",
                     "how many courses per department",
                     "is there any student with gpa above 9.9"):
        sql = compile_q(question, university).optimized_sql
        ok, why = codegen.is_single_select(sql)
        assert ok, why


def test_a_write_statement_is_refused():
    ok, why = codegen.is_single_select("DELETE FROM widget")
    assert not ok and "SELECT" in why


def test_other_dialects_render(university):
    result = compile_q("show students with gpa above 9.9", university,
                       dialect="postgres")
    assert result.dialect_sql
    sqlglot.parse_one(result.dialect_sql, read="postgres")


# ---------------------------------------------------------- equivalence
@pytest.fixture
def tiny(tmp_path):
    path = tmp_path / "tiny.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE widget (id INTEGER PRIMARY KEY, weight REAL);"
        "INSERT INTO widget VALUES (1, 1.5), (2, 2.5), (3, 2.5);"
    )
    conn.commit()
    conn.close()
    return sqlite3.connect(str(path))


def test_identical_results_pass(tiny):
    result = equivalence.check(
        tiny,
        "SELECT weight FROM widget",
        "SELECT weight FROM widget WHERE 1 = 1",
    )
    assert result.passed and result.mode == "multiset"


def test_row_order_is_ignored_without_order_by(tiny):
    result = equivalence.check(
        tiny,
        "SELECT id FROM widget",
        "SELECT id FROM widget ORDER BY id DESC",
    )
    assert result.passed


def test_multiplicity_matters(tiny):
    result = equivalence.check(
        tiny,
        "SELECT weight FROM widget",
        "SELECT DISTINCT weight FROM widget",
    )
    assert not result.passed
    assert result.n_rows_original == 3 and result.n_rows_optimized == 2
    assert result.first_difference


def test_ordered_comparison_is_used_when_both_are_ordered(tiny):
    result = equivalence.check(
        tiny,
        "SELECT id FROM widget ORDER BY id ASC",
        "SELECT id FROM widget ORDER BY id DESC",
    )
    assert result.mode == "ordered"
    assert not result.passed


def test_a_broken_query_is_reported_not_raised(tiny):
    result = equivalence.check(tiny, "SELECT 1", "SELECT * FROM nope")
    assert not result.passed and result.error


def test_floats_are_compared_with_tolerance(tiny):
    result = equivalence.check(
        tiny,
        "SELECT SUM(weight) FROM widget",
        "SELECT SUM(weight) + 0.0000000000001 FROM widget",
    )
    assert result.passed


def test_not_applicable_is_reported_clearly():
    result = equivalence.not_applicable("validated by execution")
    assert result.mode == "n/a"
    assert "validated by execution" in result.badge()
