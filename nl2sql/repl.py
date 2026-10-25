"""Interactive terminal, started by ``python main.py`` (or ``python ask.py PATH``).

Loads the database and the spaCy model once, then compiles one question per
line and prints the naive and the optimized SQL.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Callable

from .__main__ import _reconfigure_stdout
from .display import format_sql
from .lexer import load_nlp
from .pipeline import CompileResult, compile as compile_question, llm_disabled
from .schema import Database, SymbolTable, load_database

PROMPT = "Enter your query in natural language: "
ROW_LIMIT = 10
COMMANDS = ":trace  :rows  :raw  :schema  :quit"

# ANSI styles
BOLD_CYAN = "1;36"
GREEN = "32"
YELLOW = "33"
RED = "31"
DIM = "2"


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        if enabled and os.name == "nt":
            os.system("")  # switches on ANSI escape handling in the Windows console

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text


def color_wanted() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def read_line(prompt: str) -> str:
    """``input()`` that echoes piped input and drops a leading BOM."""
    line = input(prompt).lstrip("﻿")  # piped input on Windows can start with a BOM
    if not sys.stdin.isatty():
        print(line)  # piped input: show what was typed
    return line


def build_parser(prog: str = "python main.py") -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=prog,
        description="Ask questions about a database and see the SQL they compile to.",
    )
    ap.add_argument("db", nargs="?", help="path to a .db/.sqlite file or .sql dump")
    ap.add_argument("--dialect", default="sqlite", help="SQL dialect for display")
    ap.add_argument("--no-llm", action="store_true", help="never use the LLM fallback")
    ap.add_argument("--raw", action="store_true",
                    help="start with the full, fully quoted SQL (toggle with :raw)")
    return ap


def llm_available(no_llm: bool) -> bool:
    if no_llm or llm_disabled():
        return False
    from .llm_fallback import api_key

    return bool(api_key())  # only whether a key exists; the key itself is never shown


def failure_reason(result: CompileResult) -> str:
    for diag in result.diagnostics:
        if diag.level == "error":
            return diag.message
    reason = next(
        (d.message for d in result.diagnostics if d.code == "fallback"),
        "no SQL was produced",
    )
    if result.status == "fallback_unavailable":
        reason += " (LLM fallback is off)"
    return reason


def equivalence_text(result: CompileResult) -> str:
    eq = result.equivalence
    if eq is None:
        return "equivalence not checked"
    if eq.mode == "n/a":
        return "equivalence n/a"
    return "equivalent ✓" if eq.passed else "NOT equivalent ✗"


def render_result(result: CompileResult, elapsed_ms: float, dialect: str,
                  show_rows: bool, paint: Style, symbols: SymbolTable,
                  raw: bool = False) -> list[str]:
    if not result.ok:
        return [paint("could not compile: " + failure_reason(result), RED)]

    lines = [paint("-- Normal SQL --", BOLD_CYAN)]
    if result.naive_sql:
        lines.append(format_sql(result.naive_sql, dialect, symbols, raw))
    else:
        lines.append(paint("(none - answered by the LLM fallback)", DIM))
    lines.append(paint("-- Optimized SQL --", BOLD_CYAN))
    lines.append(format_sql(result.optimized_sql, dialect, symbols, raw))

    rules = [rule.split()[0] for rule in result.fired_rules()]  # "R2 semi-join" -> "R2"
    eq_ok = result.equivalence is None or result.equivalence.passed
    status = " | ".join([
        "Rules: " + (", ".join(rules) if rules else "none"),
        equivalence_text(result),
        "source: " + result.badge(),
        f"{elapsed_ms:.0f} ms",
    ])
    lines.append(paint(status, GREEN if eq_ok else RED))
    if not rules:
        lines.append(paint("No rewrite applied - query is already optimal.", YELLOW))

    if show_rows:
        lines.extend(render_rows(result, paint))
    return lines


def render_rows(result: CompileResult, paint: Style) -> list[str]:
    lines = [paint("-- Rows --", BOLD_CYAN)]
    if not result.rows:
        return lines + ["(no rows)"]
    lines.append(paint(" | ".join(result.columns), DIM))
    for row in result.rows[:ROW_LIMIT]:
        lines.append(" | ".join(str(v) for v in row))
    if len(result.rows) > ROW_LIMIT:
        lines.append(paint(f"... {len(result.rows) - ROW_LIMIT} more", DIM))
    return lines


def render_trace(result: CompileResult | None, paint: Style) -> list[str]:
    if result is None:
        return ["no question asked yet"]
    lines = [paint("-- Rewrite trace --", BOLD_CYAN)]
    if not result.trace and not result.skipped_rules:
        return lines + ["(no rule was considered)"]
    for entry in result.trace:
        lines.append(paint(f"{entry.rule} fired", GREEN) + f": {entry.note}")
        lines.extend("    " + check.render() for check in entry.checks)
    for entry in result.skipped_rules:
        lines.append(paint(f"{entry.rule} skipped", YELLOW) + f": {entry.reason()}")
        lines.extend("    " + check.render() for check in entry.checks)
    return lines


def header(db: Database, use_llm: bool, paint: Style) -> str:
    count = len(db.symbols.tables)
    noun = "table" if count == 1 else "tables"
    llm = "on" if use_llm else "off"
    return paint(f"nl2sql | {db.name} | {count} {noun} | LLM fallback {llm}", BOLD_CYAN)


def main(argv: list[str] | None = None,
         pick_database: Callable[[], str | None] | None = None,
         prog: str = "python main.py") -> int:
    """Start the REPL. Without a path, ``pick_database`` chooses one (None = quit)."""
    _reconfigure_stdout()
    parser = build_parser(prog)
    args = parser.parse_args(argv)
    paint = Style(color_wanted())

    if args.db is None:
        if pick_database is None:
            parser.error("a database path is required")
        args.db = pick_database()
        if args.db is None:
            return 0

    try:
        db = load_database(args.db)
        load_nlp()
    except Exception as exc:
        print(paint(f"cannot load {args.db}: {exc}", RED))
        return 1

    try:
        return loop(db, args.dialect, llm_available(args.no_llm), paint, args.raw)
    finally:
        db.cleanup()


def loop(db: Database, dialect: str, use_llm: bool, paint: Style,
         raw: bool = False) -> int:
    print(header(db, use_llm, paint))
    print(paint("Commands: " + COMMANDS, DIM))
    last: CompileResult | None = None
    show_rows = False

    while True:
        try:
            line = read_line(PROMPT)
        except KeyboardInterrupt:
            print()
            continue
        except EOFError:
            print()
            return 0
        text = line.strip()
        if not text:
            continue

        if text.startswith(":"):
            command = text.lower()
            if command in (":quit", ":q", ":exit"):
                return 0
            if command == ":schema":
                print(db.symbols.compact_description())
            elif command == ":rows":
                show_rows = not show_rows
                print("rows " + ("on" if show_rows else "off"))
            elif command == ":raw":
                raw = not raw
                print("raw SQL " + ("on (full quoted form)" if raw else "off (compact form)"))
            elif command == ":trace":
                print("\n".join(render_trace(last, paint)) + "\n")
            else:
                print(paint(f"unknown command {text}; try " + COMMANDS, YELLOW))
            continue

        start = time.perf_counter()
        try:
            result = compile_question(text, db, dialect=dialect, use_llm=use_llm)
        except KeyboardInterrupt:
            print(paint("cancelled", YELLOW))
            continue
        except Exception as exc:
            print(paint(f"error: {type(exc).__name__}: {exc}".splitlines()[0], RED))
            continue
        elapsed = (time.perf_counter() - start) * 1000.0
        last = result
        print("\n".join(render_result(result, elapsed, dialect, show_rows, paint,
                                      db.symbols, raw)))
        print()


if __name__ == "__main__":
    raise SystemExit(main(prog="python -m nl2sql.repl"))
