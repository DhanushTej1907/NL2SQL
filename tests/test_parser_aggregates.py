from __future__ import annotations

import pytest

from conftest import compile_q
from nl2sql.lexer import tokenize
from nl2sql.parser import parse
from nl2sql.tagger import Tagger


def ast_for(db, question):
    return parse(Tagger(db).tag(tokenize(question).tokens), question)


def sql_for(db, question):
    return compile_q(question, db).optimized_sql.upper()


def test_count_star(university):
    ast = ast_for(university, "how many students are there")
    assert [a.func for a in ast.aggregates] == ["COUNT"]
    assert ast.aggregates[0].column is None
    assert "COUNT(*)" in sql_for(university, "how many students are there")


def test_average(university):
    ast = ast_for(university, "average gpa of students")
    assert [a.func for a in ast.aggregates] == ["AVG"]
    assert "AVG(" in sql_for(university, "average gpa of students")


def test_sum(retail):
    assert "SUM(" in sql_for(retail, "total quantity of order items")


def test_min_and_max_as_value_questions(university):
    assert "MAX(" in sql_for(university, "what is the highest gpa")
    assert "MIN(" in sql_for(university, "what is the lowest gpa")


def test_superlative_on_entities_becomes_top_one(university):
    ast = ast_for(university, "which student has the highest gpa")
    assert not ast.aggregates
    assert ast.limit == 1
    assert ast.order_by and ast.order_by[0].direction == "DESC"


def test_top_k(university):
    ast = ast_for(university, "top 5 students by gpa")
    assert ast.limit == 5
    assert "top_k" in ast.intents
    assert ast.order_by[0].direction == "DESC"
    sql = sql_for(university, "top 5 students by gpa")
    assert "ORDER BY" in sql and "LIMIT 5" in sql


def test_group_by_per(university):
    ast = ast_for(university, "how many courses per department")
    assert ast.group_tables == ["department"]
    assert "GROUP BY" in sql_for(university, "how many courses per department")


def test_group_by_for_each(retail):
    assert "GROUP BY" in sql_for(retail, "how many orders for each customer")


def test_order_by_explicit(university):
    ast = ast_for(university, "show students sorted by gpa descending")
    assert ast.order_by and ast.order_by[0].direction == "DESC"
    assert "ORDER BY" in sql_for(university, "show students sorted by gpa descending")


def test_ordering_is_deterministic(university):
    sql = sql_for(university, "show students sorted by gpa descending")
    # the primary key is appended as a tiebreaker
    assert sql.count("ORDER BY") == 1
    assert sql.split("ORDER BY")[1].count(",") >= 1


def test_existence_intent(university):
    ast = ast_for(university, "is there any student with gpa above 9.9")
    assert "existence" in ast.intents


def test_semi_join_intent(university):
    ast = ast_for(university, "which departments have students with gpa over 9.9")
    assert "semi_join" in ast.intents
    assert ast.distinct


def test_distinct_intent(retail):
    ast = ast_for(retail, "list distinct cities of customers")
    assert ast.distinct and "distinct" in ast.intents


def test_selected_columns_only(university):
    sql = sql_for(university, "show the name and gpa of students")
    assert "NAME" in sql and "GPA" in sql and "CITY" not in sql


def test_default_projection_lists_every_column(university):
    sql = sql_for(university, "show students")
    assert "*" not in sql
    for column in university.symbols.table("student").column_names():
        assert column.upper() in sql


def test_two_filters_are_conjoined(university):
    sql = sql_for(university, "show students with gpa above 9 and year above 2")
    assert sql.count("AND") >= 1


def test_text_equality_filter(retail):
    sql = compile_q("show products in Electronics", retail).optimized_sql
    assert "'Electronics'" in sql


def test_parse_follows_a_renamed_schema(renamed_university):
    result = compile_q("top 5 learners by score avg", renamed_university)
    assert result.ok and result.source == "compiler"
    assert "score_avg" in result.optimized_sql
    assert len(result.rows) == 5
