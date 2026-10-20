"""The fallback, exercised with a fake client: no network, no key needed."""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from nl2sql import llm_fallback
from nl2sql.llm_fallback import extract_sql, normalize_question, resolve_model
from nl2sql.pipeline import CompileResult
from nl2sql.sql_to_ir import Unsupported, convert


@dataclass
class FakeResponse:
    text: str


class FakeModels:
    def __init__(self, replies, listing=()):
        self.replies = list(replies)
        self.listing = list(listing)
        self.prompts: list[str] = []
        self.calls = 0

    def generate_content(self, model, contents, config=None):
        self.calls += 1
        self.prompts.append(contents)
        if not self.replies:
            raise AssertionError("the fake client ran out of replies")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(reply)

    def list(self):
        return list(self.listing)


class FakeClient:
    def __init__(self, replies=(), listing=()):
        self.models = FakeModels(replies, listing)


class FakeModelInfo:
    def __init__(self, name, actions=("generateContent",)):
        self.name = f"models/{name}"
        self.supported_actions = list(actions)


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_fallback, "CACHE_DIR", tmp_path / "llm")
    monkeypatch.setattr(llm_fallback, "MODEL_CACHE", tmp_path / "llm" / "model.json")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-flash")
    yield


def fresh_result(question, db):
    return CompileResult(question=question, database=db.name, source="llm")


# ------------------------------------------------------------- helpers
def test_sql_is_extracted_from_a_code_fence():
    assert extract_sql("```sql\nSELECT 1\n```") == "SELECT 1"
    assert extract_sql("SELECT 1;") == "SELECT 1"
    assert extract_sql("```\nSELECT 1\n```") == "SELECT 1"


def test_questions_are_normalised_for_the_cache():
    assert normalize_question("  How   MANY rows? ") == "how many rows?"


def test_model_resolution_prefers_a_stable_flash(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    client = FakeClient(listing=[
        FakeModelInfo("gemini-1.5-pro"),
        FakeModelInfo("gemini-2.0-flash"),
        FakeModelInfo("gemini-2.5-flash"),
        FakeModelInfo("gemini-3.0-flash-preview"),
        FakeModelInfo("gemini-2.5-flash-lite"),
    ])
    assert resolve_model(client) == "gemini-2.5-flash"


def test_configured_model_wins(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "my-model")
    assert resolve_model(FakeClient()) == "my-model"


def test_model_resolution_survives_a_broken_listing(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)

    class Broken:
        class models:
            @staticmethod
            def list():
                raise RuntimeError("no network")

    assert resolve_model(Broken()).startswith("gemini")


# ------------------------------------------------------------- the prompt
def test_the_prompt_carries_the_schema_and_the_question(university):
    client = FakeClient(["SELECT COUNT(*) FROM student"])
    result = fresh_result("how many learners", university)
    llm_fallback.run_fallback("how many learners", university, result, client=client)
    prompt = client.models.prompts[0]
    for table in university.symbols.tables:
        assert table in prompt
    assert "how many learners" in prompt


# ------------------------------------------------------------- caching
def test_a_second_identical_question_is_served_from_the_cache(university):
    client = FakeClient(["SELECT COUNT(*) AS n FROM student"])
    question = "count every learner"
    first = fresh_result(question, university)
    llm_fallback.run_fallback(question, university, first, client=client)
    assert client.models.calls == 1

    second = fresh_result(question, university)
    llm_fallback.run_fallback(question, university, second, client=client)
    assert client.models.calls == 1, "the cached answer should be reused"
    assert any(d.code == "llm-cache" for d in second.diagnostics)
    assert second.optimized_sql


# ------------------------------------------------------------- validation
def test_a_write_statement_is_rejected(university):
    client = FakeClient(["DROP TABLE student"])
    result = fresh_result("drop everything", university)
    llm_fallback.run_fallback("drop everything", university, result, client=client)
    assert result.status == "error"
    assert any(d.code == "unsafe-sql" for d in result.diagnostics)
    assert not result.rows


def test_two_statements_are_rejected(university):
    client = FakeClient(["SELECT 1; SELECT 2"])
    result = fresh_result("two things", university)
    llm_fallback.run_fallback("two things", university, result, client=client)
    assert result.status == "error"


def test_unsupported_constructs_are_kept_but_executed(university):
    sql = (
        "WITH ranked AS (SELECT id, gpa, "
        "ROW_NUMBER() OVER (ORDER BY gpa DESC) AS rn FROM student) "
        "SELECT id, gpa FROM ranked WHERE rn <= 3"
    )
    client = FakeClient([sql])
    result = fresh_result("three best learners", university)
    llm_fallback.run_fallback("three best learners", university, result, client=client)
    assert result.optimizable is False
    assert result.status == "ok"
    assert len(result.rows) == 3
    assert result.equivalence is not None and result.equivalence.mode == "n/a"
    assert result.badge() == "LLM - unoptimized"


def test_convertible_sql_is_optimized(university):
    client = FakeClient([
        "SELECT DISTINCT department.id, department.name, department.building, "
        "department.budget FROM department "
        "JOIN student ON student.dept_id = department.id WHERE student.gpa > 9.9"
    ])
    question = "departments with a very high scoring learner"
    result = fresh_result(question, university)
    llm_fallback.run_fallback(question, university, result, client=client)
    assert result.optimizable is True
    assert any(r.startswith("R2") for r in result.fired_rules())
    assert result.equivalence is not None and result.equivalence.passed
    assert result.badge() == "LLM fallback"


# ------------------------------------------------------------- degradation
def test_no_key_degrades_gracefully(university, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(llm_fallback, "_load_env", lambda: None)
    result = fresh_result("anything at all", university)
    llm_fallback.run_fallback("anything at all", university, result)
    assert result.status == "fallback_unavailable"
    assert any("GEMINI_API_KEY" in d.message for d in result.diagnostics)


def test_a_network_error_degrades_gracefully(university):
    client = FakeClient([RuntimeError("connection reset")])
    result = fresh_result("anything", university)
    llm_fallback.run_fallback("anything", university, result, client=client)
    assert result.status == "fallback_unavailable"
    assert any("connection reset" in d.message for d in result.diagnostics)


def test_an_empty_response_degrades_gracefully(university):
    client = FakeClient([""])
    result = fresh_result("anything", university)
    llm_fallback.run_fallback("anything", university, result, client=client)
    assert result.status == "fallback_unavailable"


def test_the_key_never_reaches_the_result_or_the_cache(university, monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-value")
    client = FakeClient(["SELECT COUNT(*) FROM student"])
    result = fresh_result("count learners twice", university)
    llm_fallback.run_fallback("count learners twice", university, result, client=client)
    blob = repr(result.__dict__)
    assert "super-secret-value" not in blob
    for path in (tmp_path / "llm").glob("*.json"):
        assert "super-secret-value" not in path.read_text(encoding="utf-8")


def test_disabling_the_fallback_from_the_environment(university, monkeypatch):
    from conftest import compile_q

    monkeypatch.setenv("NL2SQL_DISABLE_LLM", "1")
    result = compile_q("please do something entirely unparseable zorblatt", university,
                       use_llm=True)
    assert result.status == "fallback_unavailable"
    assert result.source == "llm"


# ------------------------------------------------------------- SQL -> IR
def test_simple_select_converts(university):
    plan = convert("SELECT student.name FROM student WHERE student.gpa > 9",
                   university.symbols)
    assert plan.root is not None


def test_join_edges_are_matched_against_the_schema(university):
    plan = convert(
        "SELECT student.name FROM student "
        "JOIN department ON student.dept_id = department.id",
        university.symbols,
    )
    from nl2sql.ir import Join, walk_nodes

    joins = [n for n in walk_nodes(plan.root) if isinstance(n, Join)]
    assert joins and joins[0].edge is not None
    assert joins[0].edge.status == "declared"


def test_group_by_and_having_convert(university):
    plan = convert(
        "SELECT student.dept_id, COUNT(*) AS n FROM student "
        "GROUP BY student.dept_id HAVING COUNT(*) > 10",
        university.symbols,
    )
    from nl2sql.ir import Aggregate, walk_nodes

    assert any(isinstance(n, Aggregate) for n in walk_nodes(plan.root))


def test_between_and_in_convert(university):
    convert(
        "SELECT student.name FROM student WHERE student.gpa BETWEEN 8 AND 9",
        university.symbols,
    )
    convert(
        "SELECT student.name FROM student WHERE student.year IN (1, 2)",
        university.symbols,
    )


def test_order_and_limit_convert(university):
    plan = convert(
        "SELECT student.name FROM student ORDER BY student.gpa DESC LIMIT 5",
        university.symbols,
    )
    assert "top_k" in plan.intents


def test_window_functions_are_unsupported(university):
    with pytest.raises(Unsupported):
        convert(
            "SELECT id, ROW_NUMBER() OVER (ORDER BY gpa) FROM student",
            university.symbols,
        )


def test_outer_joins_are_unsupported(university):
    with pytest.raises(Unsupported):
        convert(
            "SELECT student.name FROM student "
            "LEFT JOIN department ON student.dept_id = department.id",
            university.symbols,
        )


def test_unknown_tables_are_rejected(university):
    with pytest.raises(Unsupported):
        convert("SELECT * FROM nonexistent_table", university.symbols)


@pytest.mark.live
def test_live_model_call(university):
    """Only runs when a key is present; skipped otherwise."""
    if not llm_fallback.api_key():
        pytest.skip("no GEMINI_API_KEY configured")
    os.environ.pop("GEMINI_MODEL", None)
    result = fresh_result("how many learners are registered", university)
    llm_fallback.run_fallback("how many learners are registered", university, result)
    assert result.status in ("ok", "fallback_unavailable")
