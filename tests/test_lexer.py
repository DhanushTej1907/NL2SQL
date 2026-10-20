from __future__ import annotations

import pytest

from nl2sql.lexer import model_info, tokenize


def operators(question: str) -> list[str]:
    return [t.operator for t in tokenize(question).tokens if t.is_operator]


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("more than", ">"),
        ("greater than", ">"),
        ("above", ">"),
        ("over", ">"),
        ("at least", ">="),
        ("no less than", ">="),
        ("below", "<"),
        ("under", "<"),
        ("fewer than", "<"),
        ("less than", "<"),
        ("at most", "<="),
        ("no more than", "<="),
        ("equal to", "="),
    ],
)
def test_comparison_phrases_normalise(phrase, expected):
    assert operators(f"show widgets with weight {phrase} 5") == [expected]


def test_longest_phrase_wins():
    # "no more than" must not be read as "more than".
    assert operators("widgets with weight no more than 5") == ["<="]


def test_numbers_are_parsed():
    tokens = tokenize("show 5 widgets with weight above 3.5").tokens
    values = [t.value() for t in tokens if t.is_num]
    assert 5 in values and 3.5 in values


def test_number_words_are_parsed():
    tokens = tokenize("show five widgets").tokens
    assert any(t.is_num and t.value() == 5 for t in tokens)


def test_quoted_strings_are_marked():
    tokens = tokenize('widgets named "New Delhi"').tokens
    assert any(t.is_quoted for t in tokens)


def test_tokens_carry_lemma_and_pos():
    tokens = tokenize("show the widgets").tokens
    assert all(t.lemma for t in tokens)
    assert any(t.pos for t in tokens)


def test_symbols_are_operators():
    assert operators("widgets with weight > 5") == [">"]


def test_model_loads():
    name, _vectors = model_info()
    assert name
