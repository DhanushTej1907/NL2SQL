"""The compile pipeline: question + database -> CompileResult."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import codegen, equivalence, ir
from .equivalence import EquivalenceResult
from .lexer import LexResult, tokenize
from .optimizer import OptimizeResult, TraceEntry, optimize
from .parser import ParseError, Query, parse
from .schema import Database, load_database
from .semantic import Analyzer, Diagnostic, SemanticError, SemanticResult
from .tagger import TagResult, Tagger

LOW_CONFIDENCE = 0.6
RESULT_ROW_CAP = 1000


@dataclass
class CompileResult:
    question: str
    database: str
    source: str = "compiler"          # compiler | llm
    status: str = "ok"                # ok | fallback_unavailable | error
    lex: LexResult | None = None
    tags: TagResult | None = None
    ast: Query | None = None
    plan: ir.Plan | None = None
    optimized_plan: ir.Plan | None = None
    naive_sql: str = ""
    optimized_sql: str = ""
    dialect_sql: str = ""
    dialect: str = "sqlite"
    trace: list[TraceEntry] = field(default_factory=list)
    skipped_rules: list[TraceEntry] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    plan_before: list[str] = field(default_factory=list)
    plan_after: list[str] = field(default_factory=list)
    equivalence: EquivalenceResult | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    llm_model: str = ""
    optimizable: bool = True

    @property
    def ok(self) -> bool:
        return self.status == "ok" and bool(self.optimized_sql)

    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.level == "error"]

    def fired_rules(self) -> list[str]:
        return [t.rule for t in self.trace]

    def badge(self) -> str:
        if self.source == "llm":
            if self.status != "ok":
                return "LLM fallback unavailable"
            return "LLM fallback" if self.optimizable else "LLM - unoptimized"
        return "Compiler"

    def summary(self) -> str:
        bits = [f"source={self.source}", f"status={self.status}"]
        if self.trace:
            bits.append("rules=" + ",".join(self.fired_rules()))
        if self.equivalence:
            bits.append(f"equivalence={self.equivalence.badge()}")
        return " ".join(bits)


class _Timer:
    def __init__(self, timings: dict[str, float], name: str):
        self.timings, self.name = timings, name

    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.timings[self.name] = (time.perf_counter() - self.start) * 1000.0
        return False


def llm_disabled() -> bool:
    return os.environ.get("NL2SQL_DISABLE_LLM", "") not in ("", "0", "false", "False")


def compile(question: str, db: Database | str | Path, dialect: str = "sqlite",
            use_llm: bool = True, execute: bool = True) -> CompileResult:
    """Compile one question against one database."""
    if not isinstance(db, Database):
        db = load_database(db)

    result = CompileResult(question=question, database=db.name, dialect=dialect)
    for message in db.load_diagnostics:
        result.diagnostics.append(Diagnostic("warning", "dump-load", message))

    fallback_reason = ""
    try:
        with _Timer(result.timings, "lex"):
            result.lex = tokenize(question)
        with _Timer(result.timings, "tag"):
            tagger = Tagger(db)
            result.tags = tagger.tag(result.lex.tokens)
        with _Timer(result.timings, "parse"):
            result.ast = parse(result.tags, question)
        with _Timer(result.timings, "semantic"):
            analysis = Analyzer(db).analyze(result.ast)
        result.diagnostics.extend(analysis.diagnostics)

        confidence = result.tags.min_schema_confidence()
        if confidence < LOW_CONFIDENCE:
            fallback_reason = f"tagger confidence {confidence:.2f} below {LOW_CONFIDENCE}"
        elif result.tags.unmatched:
            words = ", ".join(sorted({t.text for t in result.tags.unmatched}))
            fallback_reason = f"unmatched words in the question: {words}"

        if not fallback_reason:
            with _Timer(result.timings, "lower"):
                result.plan = ir.lower(result.ast, analysis, db.symbols)
    except ParseError as exc:
        fallback_reason = f"parse error: {exc.message}"
        result.diagnostics.append(
            Diagnostic("warning", "parse-error", exc.message, exc.span)
        )
    except SemanticError as exc:
        fallback_reason = f"semantic error: {exc}"
        result.diagnostics.extend(exc.diagnostics)

    if fallback_reason:
        return _fallback(question, db, result, fallback_reason, dialect, use_llm, execute)

    _optimize_and_run(result, db, dialect, execute)
    return result


def _optimize_and_run(result: CompileResult, db: Database, dialect: str,
                      execute: bool) -> CompileResult:
    assert result.plan is not None
    with _Timer(result.timings, "codegen_naive"):
        result.naive_sql = codegen.generate(result.plan, "sqlite")

    with _Timer(result.timings, "optimize"):
        opt = optimize(result.plan, db.symbols)
    result.optimized_plan = opt.plan
    result.trace = opt.trace
    result.skipped_rules = opt.skipped

    with _Timer(result.timings, "codegen_optimized"):
        result.optimized_sql = codegen.generate(opt.plan, "sqlite")
        if dialect != "sqlite":
            try:
                result.dialect_sql = codegen.to_dialect(result.optimized_sql, dialect)
            except Exception as exc:
                result.dialect_sql = ""
                result.diagnostics.append(
                    Diagnostic("warning", "dialect", f"cannot render {dialect}: {exc}")
                )
        else:
            result.dialect_sql = result.optimized_sql

    if execute:
        _execute(result, db)
    return result


def _execute(result: CompileResult, db: Database) -> None:
    ok, why = codegen.is_single_select(result.optimized_sql)
    if not ok:
        result.status = "error"
        result.diagnostics.append(Diagnostic("error", "unsafe-sql", why))
        return
    conn = db.connect()
    try:
        with _Timer(result.timings, "execute"):
            result.columns, rows = equivalence.run_query(conn, result.optimized_sql)
            result.rows = rows[:RESULT_ROW_CAP]
        result.plan_before = codegen.explain_plan(conn, result.naive_sql)
        result.plan_after = codegen.explain_plan(conn, result.optimized_sql)
        with _Timer(result.timings, "equivalence"):
            if result.naive_sql and result.naive_sql != result.optimized_sql:
                result.equivalence = equivalence.check(
                    conn, result.naive_sql, result.optimized_sql
                )
            elif result.naive_sql:
                result.equivalence = equivalence.EquivalenceResult(
                    True, "multiset", len(result.rows), len(result.rows),
                    first_difference="no rewrite fired, so the queries are identical",
                )
            else:
                result.equivalence = equivalence.not_applicable(
                    "validated by execution"
                )
    except Exception as exc:
        result.status = "error"
        result.diagnostics.append(Diagnostic("error", "execution", str(exc)))
    finally:
        conn.close()


def _fallback(question: str, db: Database, result: CompileResult, reason: str,
              dialect: str, use_llm: bool, execute: bool) -> CompileResult:
    result.source = "llm"
    result.diagnostics.append(Diagnostic("info", "fallback", reason))
    if not use_llm or llm_disabled():
        result.status = "fallback_unavailable"
        result.diagnostics.append(
            Diagnostic("info", "fallback-disabled",
                       "the language-model fallback is switched off")
        )
        return result

    from .llm_fallback import run_fallback

    with _Timer(result.timings, "llm"):
        run_fallback(question, db, result, dialect=dialect, execute=execute)
    return result
