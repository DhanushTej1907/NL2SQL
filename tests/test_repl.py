"""The terminal REPL and its launchers, driven with scripted input."""

from __future__ import annotations

import os
import subprocess
import sys

from conftest import ROOT, SAMPLES

Q_R2 = "which departments have students with gpa over 9.9\n"


def _env() -> dict:
    env = dict(os.environ, NL2SQL_DISABLE_LLM="1", PYTHONIOENCODING="utf-8")
    env.pop("NO_COLOR", None)
    return env


def run(args: list[str], script: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args], input=script, capture_output=True, text=True,
        encoding="utf-8", env=_env(), cwd=str(ROOT), timeout=180,
    )


def run_repl(script: str, db: str = "university.db") -> subprocess.CompletedProcess:
    return run([str(ROOT / "ask.py"), str(SAMPLES / db)], script)


def rules_line(out: str) -> str:
    return next(line for line in out.splitlines() if line.startswith("Rules:"))


def test_question_prints_both_sql_sections_and_r2_fires():
    proc = run_repl(Q_R2 + ":trace\n:rows\n:quit\n")
    out = proc.stdout
    assert proc.returncode == 0, proc.stderr
    assert "LLM fallback off" in out
    assert "Enter your query in natural language: " in out
    assert "-- Normal SQL --" in out
    assert "-- Optimized SQL --" in out
    assert "R2" in rules_line(out)
    assert any(l.startswith("R2") and " fired:" in l for l in out.splitlines())  # :trace
    assert "rows on" in out            # :rows
    assert "Traceback" not in out + proc.stderr
    assert "\033[" not in out          # piped output is not a TTY: no color


def test_compact_display_and_raw_toggle():
    proc = run_repl(Q_R2 + ":raw\n" + Q_R2 + ":quit\n")
    assert proc.returncode == 0, proc.stderr
    compact, raw = proc.stdout.split("raw SQL on")
    assert "department.*" in compact                   # join: table.*
    assert "SELECT\n    *\n  FROM student" in compact  # single table: *
    assert '"' not in compact                          # no needless quotes
    assert "student.gpa > 9.9 AND student.dept_id = department.id" in compact
    assert '"department"."budget"' in raw              # :raw is the full form
    assert '(\n        "student"."gpa" > 9.9\n      ) AND' in raw


def test_compact_keeps_quotes_where_required(university):
    from sqlglot.dialects.dialect import Dialect

    from nl2sql.display import _needs_quotes, compact_sql

    sqlite = Dialect.get_or_raise("sqlite")
    assert not _needs_quotes("gpa", sqlite)
    assert _needs_quotes("select", sqlite)
    assert _needs_quotes("order", sqlite)
    assert _needs_quotes("first name", sqlite)
    assert _needs_quotes("AlbumId", sqlite)

    sql = 'SELECT "t"."select", "t"."Mixed" FROM "t" WHERE (("t"."a" = 1) OR ("t"."b" = 2))'
    out = compact_sql(sql, "sqlite", university.symbols)
    assert 't."select"' in out and 't."Mixed"' in out
    # The parentheses around the whole condition go; the OR operands keep theirs.
    assert " ".join(out.split()).endswith("WHERE ( t.a = 1 ) OR ( t.b = 2 )")


def test_failure_is_one_line_and_eof_exits_cleanly():
    proc = run_repl("zzqx blorf wibble\n")
    assert proc.returncode == 0, proc.stderr
    failures = [l for l in proc.stdout.splitlines() if "could not compile:" in l]
    assert len(failures) == 1
    assert "Traceback" not in proc.stdout + proc.stderr


def test_main_menu_enter_picks_the_default_sample():
    proc = run(["main.py"], "\n" + Q_R2 + ":quit\n")
    out = proc.stdout
    assert proc.returncode == 0, proc.stderr
    assert "Choose a database:" in out
    assert "university.db  (default)" in out
    assert "Type a path" in out
    assert "nl2sql | university.db |" in out
    assert "R2" in rules_line(out)


def test_main_with_a_path_skips_the_menu():
    proc = run(["main.py", str(SAMPLES / "retail.db")], ":quit\n")
    assert proc.returncode == 0, proc.stderr
    assert "Choose a database:" not in proc.stdout
    assert "nl2sql | retail.db |" in proc.stdout


def test_main_menu_by_number_and_eof_quits():
    listing = run(["main.py"], "")
    assert listing.returncode == 0, listing.stderr   # EOF at the menu: clean exit
    number = next(l.split(".")[0].strip() for l in listing.stdout.splitlines()
                  if l.strip().endswith("retail.db"))
    proc = run(["main.py"], f"{number}\n:quit\n")
    assert proc.returncode == 0, proc.stderr
    assert "nl2sql | retail.db |" in proc.stdout


def test_cli_compact_by_default_and_raw_flag():
    base = ["-m", "nl2sql", Q_R2.strip(), "--db", str(SAMPLES / "university.db"),
            "--no-llm"]
    compact = run(base)
    raw = run(base + ["--raw"])
    assert compact.returncode == 0 and raw.returncode == 0, compact.stderr + raw.stderr
    assert "department.*" in compact.stdout and '"' not in compact.stdout.split(
        "optimized SQL:")[0]
    assert '"department"."budget"' in raw.stdout
