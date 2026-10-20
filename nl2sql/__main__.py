"""Command-line entry point: ``python -m nl2sql "question" --db PATH``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import ir
from .pipeline import compile as compile_question
from .schema import load_database

SHOWABLE = ("tokens", "tags", "ast", "ir", "optimized", "trace", "plan",
            "schema", "diagnostics")


def _reconfigure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m nl2sql",
        description="Compile a natural-language question into SQL.",
    )
    ap.add_argument("question", help="the question to compile")
    ap.add_argument("--db", required=True, help="path to a .db/.sqlite file or .sql dump")
    ap.add_argument("--dialect", default="sqlite", help="SQL dialect for display")
    ap.add_argument("--show", default="", help="comma-separated: " + ", ".join(SHOWABLE))
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-llm", action="store_true", help="never use the LLM fallback")
    ap.add_argument("--rows", type=int, default=20, help="result rows to print")
    return ap


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdout()
    args = build_parser().parse_args(argv)
    show = {s.strip() for s in args.show.split(",") if s.strip()}

    db = load_database(args.db)
    result = compile_question(args.question, db, dialect=args.dialect,
                              use_llm=not args.no_llm)

    if args.json:
        payload = {
            "question": result.question,
            "source": result.source,
            "status": result.status,
            "naive_sql": result.naive_sql,
            "optimized_sql": result.optimized_sql,
            "dialect_sql": result.dialect_sql,
            "rules": result.fired_rules(),
            "equivalence": result.equivalence.badge() if result.equivalence else None,
            "diagnostics": [d.render() for d in result.diagnostics],
            "columns": result.columns,
            "rows": [list(r) for r in result.rows[: args.rows]],
            "timings_ms": {k: round(v, 2) for k, v in result.timings.items()},
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0 if result.ok else 1

    print(f"question : {result.question}")
    print(f"database : {result.database}")
    print(f"source   : {result.badge()}")

    if "schema" in show:
        print()
        print(db.symbols.compact_description())
    if "tokens" in show and result.lex:
        print()
        print("tokens   : " + ", ".join(t.text for t in result.lex.tokens))
    if "tags" in show and result.tags:
        print()
        print("tags:")
        for tag in result.tags.tags:
            print(f"  {tag.start}:{tag.end} {tag.label()}  "
                  f"{tag.confidence:.2f}  {tag.how}")
    if "ast" in show and result.ast:
        print()
        print(result.ast.pretty())
    if "ir" in show and result.plan:
        print()
        print("naive IR:")
        print(result.plan.pretty())
    if "optimized" in show and result.optimized_plan:
        print()
        print("optimized IR:")
        print(result.optimized_plan.pretty())

    print()
    print("naive SQL:")
    print("  " + result.naive_sql)
    print("optimized SQL:")
    print("  " + (result.dialect_sql or result.optimized_sql))

    if "trace" in show:
        print()
        print("rewrite trace:")
        if not result.trace:
            print("  (no rule fired)")
        for entry in result.trace:
            print(f"  {entry.rule}: {entry.note}")
            for check in entry.checks:
                print(f"    {check.render()}")
        for entry in result.skipped_rules:
            print(f"  skipped {entry.rule}: {entry.reason()}")

    if "plan" in show:
        print()
        print("plan before: " + "; ".join(result.plan_before))
        print("plan after : " + "; ".join(result.plan_after))

    if "diagnostics" in show or result.errors():
        print()
        for diag in result.diagnostics:
            print("  " + diag.render())

    if result.equivalence:
        print()
        print("equivalence: " + result.equivalence.render())

    if result.rows:
        print()
        print("rows (" + ", ".join(result.columns) + "):")
        for row in result.rows[: args.rows]:
            print("  " + " | ".join(str(v) for v in row))
        if len(result.rows) > args.rows:
            print(f"  ... {len(result.rows) - args.rows} more")
    elif result.ok:
        print()
        print("rows: (none)")

    timings = ", ".join(f"{k} {v:.1f}ms" for k, v in result.timings.items())
    print()
    print("timings: " + timings)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
