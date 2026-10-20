from __future__ import annotations

from nl2sql.lexer import tokenize
from nl2sql.tagger import Tagger, split_identifier


def tags_for(db, question):
    return Tagger(db).tag(tokenize(question).tokens)


def labels(db, question):
    return [t.label() for t in tags_for(db, question).tags]


def test_split_identifier_handles_every_style():
    assert split_identifier("order_items") == ["order", "items"]
    assert split_identifier("OrderItems") == ["order", "items"]
    assert split_identifier("orderItems") == ["order", "items"]
    assert split_identifier("InvoiceLineId") == ["invoice", "line", "id"]


def test_plural_table_name_matches(university):
    assert "table:student" in labels(university, "show students")


def test_column_name_matches_exactly(university):
    assert "column:student.gpa" in labels(university, "show students with gpa above 9")


def test_value_lookup_finds_a_sampled_value(university):
    found = [t for t in tags_for(university, "students in CSE").tags
             if t.kind == "value"]
    assert found and found[0].value == "CSE"
    assert found[0].confidence >= 0.85


def test_underscored_table_name_matches_two_words(retail):
    assert "table:order_items" in labels(retail, "how many order items are there")


def test_aggregate_keywords_are_tagged(university):
    assert "agg:COUNT" in labels(university, "how many students are there")
    assert "agg:AVG" in labels(university, "average gpa of students")


def test_grammar_words_are_not_tagged_as_values(university):
    # The article "a" must not match the grade value 'A'.
    found = [t for t in tags_for(university, "show a student").tags
             if t.kind == "value"]
    assert not found


def test_plural_table_beats_its_grammar_lemma(retail):
    # "orders" lemmatises to the ordering keyword "order".
    assert "table:orders" in labels(retail, "how many orders per customer")


def test_every_tag_carries_a_confidence(university):
    for tag in tags_for(university, "show students with gpa above 9").tags:
        assert 0.0 < tag.confidence <= 1.0
        assert tag.how


def test_abbreviation_matching(retail):
    found = [t for t in tags_for(retail, "show the qty of order items").tags
             if t.kind == "column"]
    assert any(t.column == "quantity" for t in found)


def test_unknown_words_are_reported_as_unmatched(university):
    result = tags_for(university, "show the zorblatt of students")
    assert any(t.text == "zorblatt" for t in result.unmatched)


def test_tagging_follows_a_renamed_schema(renamed_university):
    found = labels(renamed_university, "show learners with score avg above 9")
    assert "table:learner" in found
    assert "column:learner.score_avg" in found
