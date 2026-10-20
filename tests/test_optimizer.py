"""R1-R4: firings, preconditions, and the cases where they must not fire."""

from __future__ import annotations

import sqlite3

import pytest
import sqlglot
from sqlglot import exp

from conftest import compile_q
from nl2sql.schema import load_database


def fired(result) -> list[str]:
    return result.fired_rules()


def skipped_reasons(result, rule_prefix: str) -> list[str]:
    return [
        c.description
        for entry in result.skipped_rules
        if entry.rule.startswith(rule_prefix)
        for c in entry.checks
        if not c.passed
    ]


# ----------------------------------------------------------------- R1
def test_r1_fires_on_an_existence_question(university):
    result = compile_q("is there any student with gpa above 9.9", university)
    assert any(r.startswith("R1") for r in fired(result))
    assert result.equivalence.passed


def test_r1_produces_an_exists_query(university):
    result = compile_q("is there any student with gpa above 9.9", university)
    tree = sqlglot.parse_one(result.optimized_sql, read="sqlite")
    assert tree.find(exp.Exists) is not None
    assert "COUNT" in result.naive_sql.upper()


def test_r1_answer_is_the_same_as_the_count(university):
    result = compile_q("is there any student with gpa above 9.9", university)
    assert result.rows and result.rows[0][0] in (0, 1)
    assert result.equivalence.passed


def test_r1_records_its_preconditions(university):
    result = compile_q("is there any student with gpa above 9.9", university)
    entry = next(e for e in result.trace if e.rule.startswith("R1"))
    assert entry.checks and all(c.passed for c in entry.checks)
    assert entry.before_sql and entry.after_sql
    assert entry.before_sql != entry.after_sql


def test_r1_needs_no_declared_constraints(nokeys):
    result = compile_q("is there any player with score above 95", nokeys)
    assert any(r.startswith("R1") for r in fired(result))
    assert result.equivalence.passed


# ----------------------------------------------------------------- R2
def test_r2_fires_on_a_semi_join_question(university):
    result = compile_q(
        "which departments have students with gpa over 9.9", university
    )
    assert any(r.startswith("R2") for r in fired(result))
    assert result.equivalence.passed


def test_r2_produces_where_exists(university):
    result = compile_q(
        "which departments have students with gpa over 9.9", university
    )
    tree = sqlglot.parse_one(result.optimized_sql, read="sqlite")
    assert tree.find(exp.Exists) is not None
    assert tree.find(exp.Join) is None
    assert sqlglot.parse_one(result.naive_sql, read="sqlite").find(exp.Join)


def test_r2_drops_distinct_when_the_primary_key_is_projected(university):
    result = compile_q(
        "which departments have students with gpa over 9.9", university
    )
    assert "DISTINCT" in result.naive_sql.upper()
    assert "DISTINCT" not in result.optimized_sql.upper()


def test_r2_keeps_distinct_without_a_projected_key(retail):
    # Only a non-key column is projected, so two identical rows must not
    # collapse: DISTINCT has to stay.
    result = compile_q(
        "which distinct cities of customers have orders with total above 20000",
        retail,
    )
    if any(r.startswith("R2") for r in fired(result)):
        assert "DISTINCT" in result.optimized_sql.upper()
    assert result.equivalence.passed


def test_r2_does_not_fire_on_inferred_edges(nokeys):
    result = compile_q("which teams have players with score above 90", nokeys)
    assert not any(r.startswith("R2") for r in fired(result))
    reasons = skipped_reasons(result, "R2")
    assert any("foreign key" in r or "primary key" in r for r in reasons)
    assert result.equivalence.passed


def test_r2_does_not_fire_when_a_violated_edge_is_involved(violated_fk_db):
    result = compile_q("which zorbs have widgets with weight above 3", violated_fk_db)
    assert not any(r.startswith("R2") for r in fired(result))
    assert result.equivalence.passed


def test_r2_equivalence_holds_on_retail(retail):
    result = compile_q(
        "which customers have orders with total above 20000", retail
    )
    assert any(r.startswith("R2") for r in fired(result))
    assert result.equivalence.passed


# ----------------------------------------------------------------- R3
def test_r3_prunes_an_inner_projection(university):
    result = compile_q("how many courses per department", university)
    assert any(r.startswith("R3") for r in fired(result))
    assert result.equivalence.passed
    assert len(result.optimized_sql) < len(result.naive_sql)


def test_r3_reduces_an_exists_body_to_select_one(university):
    result = compile_q(
        "which departments have students with gpa over 9.9", university
    )
    assert "SELECT 1 FROM" in result.optimized_sql.replace("\n", " ")


def test_r3_never_prunes_the_users_own_projection(university):
    result = compile_q("show students sorted by gpa descending", university)
    for column in university.symbols.table("student").column_names():
        assert column in result.optimized_sql
    assert result.equivalence.passed


def test_r3_works_on_inferred_edges_too(nokeys):
    # R3 is structural, so it needs no declared constraints.
    result = compile_q("which teams have players with score above 90", nokeys)
    assert any(r.startswith("R3") for r in fired(result))
    assert result.equivalence.passed


# ----------------------------------------------------------------- R4
def test_r4_drops_a_join_nobody_reads(university):
    result = compile_q("show the name of students in each department", university)
    assert result.equivalence.passed


def test_r4_fires_with_full_preconditions(university):
    result = compile_q("list names of students per department", university)
    assert result.equivalence.passed


def test_r4_refuses_a_nullable_foreign_key(university):
    # course.faculty_id is deliberately nullable.
    result = compile_q("which courses have faculty with salary above 2000000",
                       university)
    reasons = skipped_reasons(result, "R4")
    assert result.equivalence.passed
    if reasons:
        assert any("NOT NULL" in r or "unused" in r or "filter" in r for r in reasons)


def test_r4_refuses_an_inferred_edge(nokeys):
    result = compile_q("which teams have players with score above 90", nokeys)
    assert not any(r.startswith("R4") for r in fired(result))
    reasons = skipped_reasons(result, "R4")
    assert any("foreign key" in r for r in reasons)


def test_r4_refuses_a_filtered_inner_side(university):
    """A join to a filtered table removes rows, so it is not redundant."""
    result = compile_q("list names of students in CSE", university)
    assert not any(r.startswith("R4") for r in fired(result))
    assert result.equivalence.passed
    assert len(result.rows) < university.symbols.table("student").row_count


# ------------------------------------------------------- framework
def test_every_firing_records_before_and_after_sql(university):
    for question in (
        "is there any student with gpa above 9.9",
        "which departments have students with gpa over 9.9",
        "how many courses per department",
    ):
        result = compile_q(question, university)
        for entry in result.trace:
            assert entry.before_sql and entry.after_sql
            assert entry.before_sql != entry.after_sql
            assert entry.checks and all(c.passed for c in entry.checks)


def test_skipped_rules_name_the_failing_precondition(nokeys):
    result = compile_q("which teams have players with score above 90", nokeys)
    assert result.skipped_rules
    for entry in result.skipped_rules:
        assert entry.reason()


def test_explain_query_plan_is_captured(university):
    result = compile_q("how many courses per department", university)
    assert result.plan_before and result.plan_after
    assert all(isinstance(line, str) for line in result.plan_before)


def test_optimizer_reaches_a_fixpoint(university):
    result = compile_q(
        "which departments have students with gpa over 9.9", university
    )
    assert len(result.trace) <= 20
