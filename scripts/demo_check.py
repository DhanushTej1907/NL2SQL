"""Run every question in ``demo_questions.md`` and report PASS or FAIL.

A compiler question passes when it compiles without the language model,
executes, passes the equivalence check, and fires every rule annotated for
it. An LLM question passes when the fallback answered and validated it, or
when the fallback was unavailable and said so cleanly.

Exit code is non-zero if any question fails.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nl2sql.pipeline import compile as compile_question  # noqa: E402
from nl2sql.schema import load_database  # noqa: E402

QUESTIONS_FILE = ROOT / "demo_questions.md"
SAMPLES = ROOT / "samples"

_DB_HEADING = re.compile(r"^##\s+(\S+\.(?:db|sqlite|sqlite3|sql))\s*$")
_SECTION = re.compile(r"^###\s+(compiler|llm)\s*$", re.I)
_ITEM = re.compile(r"^-\s+(.*?)\s*(?:<!--\s*expect:\s*(.*?)\s*-->)?\s*$")


@dataclass
class Item:
    database: str
    section: str
    question: str
    expected_rules: list[str]


def parse_questions(path: Path) -> list[Item]:
    items: list[Item] = []
    database = ""
    section = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        heading = _DB_HEADING.match(line)
        if heading:
            database, section = heading.group(1), ""
            continue
        marker = _SECTION.match(line)
        if marker:
            section = marker.group(1).lower()
            continue
        if not line.startswith("- ") or not database or not section:
            continue
        match = _ITEM.match(line)
        if not match:
            continue
        question = match.group(1).strip()
        rules = [
            r.strip().upper()
            for r in (match.group(2) or "").split(",")
            if r.strip()
        ]
        if question:
            items.append(Item(database, section, question, rules))
    return items


def check(item: Item, db) -> tuple[bool, str]:
    use_llm = item.section == "llm"
    try:
        result = compile_question(item.question, db, use_llm=use_llm)
    except Exception as exc:
        return False, f"crashed: {type(exc).__name__}: {exc}"

    if item.section == "llm":
        if result.status == "fallback_unavailable":
            reason = next(
                (d.message for d in result.diagnostics
                 if d.code in ("fallback-unavailable", "fallback-disabled")),
                "unavailable",
            )
            return True, f"fallback unavailable, handled cleanly ({reason})"
        if result.status != "ok":
            return False, f"status={result.status}"
        if not result.optimized_sql:
            return False, "the fallback produced no SQL"
        if result.source != "llm":
            # The question was meant to fall outside the grammar but the
            # compiler took it; say so rather than quietly counting it.
            return False, ("the compiler handled it, so this question does "
                           "not exercise the fallback")
        kind = "optimized" if result.optimizable else "kept as written"
        return True, f"llm answered ({kind}), {len(result.rows)} rows"

    if result.source != "compiler":
        reason = next((d.message for d in result.diagnostics
                       if d.code == "fallback"), "fell back")
        return False, f"used the fallback: {reason}"
    if result.status != "ok":
        errors = "; ".join(d.message for d in result.errors())
        return False, f"status={result.status} {errors}"
    if result.equivalence is None or not result.equivalence.passed:
        detail = result.equivalence.render() if result.equivalence else "not run"
        return False, f"equivalence failed: {detail}"

    fired = {r.split()[0].upper() for r in result.fired_rules()}
    missing = [r for r in item.expected_rules if r not in fired]
    if missing:
        return False, f"expected {', '.join(missing)} to fire, got {sorted(fired) or 'none'}"

    bits = [f"{len(result.rows)} rows"]
    if fired:
        bits.append("rules " + ",".join(sorted(fired)))
    if result.skipped_rules:
        bits.append(f"{len(result.skipped_rules)} skipped")
    return True, ", ".join(bits)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", action="append",
                    help="only check this database (repeatable)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    items = parse_questions(QUESTIONS_FILE)
    if not items:
        print("no questions found in demo_questions.md")
        return 1
    if args.db:
        wanted = set(args.db)
        items = [i for i in items if i.database in wanted]

    failures = 0
    loaded: dict[str, object] = {}
    current = ""

    for item in items:
        if item.database != current:
            current = item.database
            print(f"\n=== {current} ===")
        if item.database not in loaded:
            path = SAMPLES / item.database
            if not path.exists():
                print(f"SKIP  {item.database} is missing")
                loaded[item.database] = None
                continue
            loaded[item.database] = load_database(path)
        db = loaded[item.database]
        if db is None:
            continue

        start = time.perf_counter()
        passed, detail = check(item, db)
        elapsed = (time.perf_counter() - start) * 1000
        status = "PASS" if passed else "FAIL"
        if not passed:
            failures += 1
        print(f"{status}  [{item.section}] {item.question}")
        if not args.quiet:
            print(f"      {detail}  ({elapsed:.0f} ms)")

    total = len([i for i in items if loaded.get(i.database) is not None])
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
