from __future__ import annotations

from pathlib import Path

import pytest

streamlit_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = streamlit_testing.AppTest

APP = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture(scope="module")
def app():
    at = AppTest.from_file(str(APP), default_timeout=300)
    at.run()
    return at


def test_app_starts_without_error(app):
    assert app.exception == []
    assert "NL2SQL" in app.title[0].value


def test_samples_are_discovered(app):
    options = app.sidebar.selectbox[0].options
    assert any(name.endswith(".db") for name in options)
    assert any(name.endswith(".sql") for name in options)


def _run_question(question: str, sample: str = "university.db"):
    at = AppTest.from_file(str(APP), default_timeout=300)
    at.run()
    at.sidebar.selectbox[0].set_value(sample).run()
    at.text_input[0].set_value(question).run()
    return at


def test_a_question_compiles_and_shows_every_tab():
    at = _run_question("which departments have students with gpa over 9.9")
    assert at.exception == []
    labels = [t.label for t in at.tabs]
    for expected in ("Results", "Tokens & tags", "AST", "IR", "Optimized IR",
                     "SQL", "Rewrite trace", "Plan before/after"):
        assert expected in labels


def test_the_source_badge_says_compiler():
    at = _run_question("which departments have students with gpa over 9.9")
    messages = [s.value for s in at.success]
    assert any("Source: Compiler" in m for m in messages)


def test_the_equivalence_badge_is_shown():
    at = _run_question("which departments have students with gpa over 9.9")
    messages = [s.value for s in at.success]
    assert any("Equivalence: equivalent" in m for m in messages)


def test_results_are_rendered():
    at = _run_question("show students with gpa above 9.9")
    assert at.exception == []
    assert len(at.dataframe) >= 1


def test_naive_and_optimized_sql_are_both_shown():
    at = _run_question("which departments have students with gpa over 9.9")
    code = "\n".join(block.value for block in at.code)
    assert "DISTINCT" in code.upper()     # the naive form
    assert "EXISTS" in code.upper()       # the optimized form


def test_the_rewrite_trace_lists_preconditions():
    at = _run_question("which departments have students with gpa over 9.9")
    text = "\n".join(
        [m.value for m in at.markdown]
        + [s.value for s in at.success]
        + [c.value for c in at.caption]
    )
    assert "R2 semi-join" in text
    assert "declared" in text


def test_another_sample_database_also_works():
    at = _run_question("how many orders per customer", sample="retail.db")
    assert at.exception == []
    assert len(at.dataframe) >= 1
