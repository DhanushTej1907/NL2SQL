from __future__ import annotations

import pytest

from conftest import compile_q
from nl2sql.lexer import tokenize
from nl2sql.parser import parse
from nl2sql.semantic import Analyzer, SemanticError, build_join_path, shortest_path
from nl2sql.tagger import Tagger


def analyze(db, question):
    ast = parse(Tagger(db).tag(tokenize(question).tokens), question)
    return Analyzer(db).analyze(ast)


def test_numeric_comparator_on_text_is_an_error(university):
    with pytest.raises(SemanticError) as excinfo:
        analyze(university, "show students with name above 5")
    assert any(d.code == "type-mismatch" for d in excinfo.value.diagnostics)


def test_non_numeric_literal_against_a_numeric_column_is_an_error(university):
    with pytest.raises(SemanticError) as excinfo:
        analyze(university, 'show students with gpa equal to "Mumbai"')
    assert any(d.code == "type-mismatch" for d in excinfo.value.diagnostics)


def test_misspelling_produces_a_suggestion(university):
    suggestions = Analyzer(university)._suggest("studnet")
    assert "student" in suggestions


def test_one_hop_join_path(university):
    result = analyze(university, "show students in CSE")
    assert len(result.join_path.edges) == 1
    assert result.join_path.trusted()


def test_two_hop_join_path(university):
    edges = shortest_path(university.symbols, "student", "course")
    assert edges is not None and len(edges) == 2


def test_join_path_is_reported_as_a_diagnostic(university):
    result = analyze(university, "show students in CSE")
    assert any(d.code == "join-path" for d in result.diagnostics)


def test_inferred_joins_are_flagged(nokeys):
    result = analyze(nokeys, "which teams have players with score above 90")
    assert any(d.code == "inferred-join" for d in result.diagnostics)
    assert not result.join_path.trusted()


def test_violated_edges_are_not_trusted(violated_fk_db):
    path, diagnostics = build_join_path(violated_fk_db.symbols, "widget", ["zorb"])
    assert not path.trusted()
    assert any(d.code == "violated-fk" for d in diagnostics)


def test_ambiguous_column_prefers_the_target_table(university):
    result = analyze(university, "show the name of students")
    projected = result.query.projections[0]
    assert projected.table == "student" and projected.column == "name"


def test_ambiguous_column_resolves_to_a_joined_table(university):
    # "building" exists only on one table, so it pulls that table in.
    result = analyze(university, "show students in Block A")
    assert "department" in result.join_path.tables


def test_count_is_allowed_on_a_text_column(university):
    result = compile_q("how many students are there", university)
    assert result.ok


def test_semantics_follow_a_renamed_schema(renamed_university):
    result = analyze(renamed_university, "show learners in CSE")
    assert result.join_path.edges
    assert result.query.target == "learner"
