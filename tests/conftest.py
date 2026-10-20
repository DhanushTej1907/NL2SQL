from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
sys.path.insert(0, str(ROOT))

# Tests never call the network; the fallback is exercised with a fake client.
os.environ.setdefault("NL2SQL_DISABLE_LLM", "1")

from nl2sql.lexer import load_nlp  # noqa: E402
from nl2sql.schema import load_database  # noqa: E402


def _ensure_samples() -> None:
    needed = ["university.db", "retail.db", "nokeys.db", "retail_dump.sql"]
    if all((SAMPLES / n).exists() for n in needed):
        return
    sys.path.insert(0, str(ROOT / "scripts"))
    import make_samples

    for builder in make_samples.BUILDERS.values():
        builder()


@pytest.fixture(scope="session", autouse=True)
def _nlp():
    _ensure_samples()
    return load_nlp()


@pytest.fixture(scope="session")
def university():
    return load_database(SAMPLES / "university.db")


@pytest.fixture(scope="session")
def retail():
    return load_database(SAMPLES / "retail.db")


@pytest.fixture(scope="session")
def nokeys():
    return load_database(SAMPLES / "nokeys.db")


@pytest.fixture(scope="session")
def renamed_university(tmp_path_factory):
    """A copy of the university schema with every name changed.

    Nothing in ``nl2sql/`` may depend on the original names, so the same
    questions must still compile after the rename.
    """
    src = SAMPLES / "university.db"
    out_dir = tmp_path_factory.mktemp("renamed")
    dst = out_dir / "renamed.db"
    shutil.copyfile(src, dst)

    renames = {
        "department": "faculty_unit",
        "student": "learner",
        "faculty": "instructor",
        "course": "module",
        "enrollment": "signup",
    }
    columns = {
        ("learner", "gpa"): "score_avg",
        ("learner", "city"): "home_town",
        ("module", "title"): "label",
        ("module", "credits"): "weight",
        ("instructor", "salary"): "pay",
    }
    conn = sqlite3.connect(str(dst))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        for old, new in renames.items():
            conn.execute(f'ALTER TABLE "{old}" RENAME TO "{new}"')
        for (table, old), new in columns.items():
            conn.execute(f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"')
        conn.commit()
    finally:
        conn.close()
    return load_database(dst)


@pytest.fixture(scope="session")
def violated_fk_db(tmp_path_factory):
    """A schema with a declared FK the data breaks, so it must be demoted."""
    path = tmp_path_factory.mktemp("violated") / "violated.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE zorb (
            id   INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE TABLE widget (
            id      INTEGER PRIMARY KEY,
            label   TEXT NOT NULL,
            zorb_id INTEGER NOT NULL REFERENCES zorb(id),
            weight  REAL
        );
        INSERT INTO zorb VALUES (1, 'alpha'), (2, 'beta');
        INSERT INTO widget VALUES (1, 'one', 1, 2.5), (2, 'two', 2, 4.0),
                                  (3, 'three', 99, 7.5);
        """
    )
    conn.commit()
    conn.close()
    return load_database(path)


def compile_q(question, db, **kwargs):
    from nl2sql.pipeline import compile as compile_question

    kwargs.setdefault("use_llm", False)
    return compile_question(question, db, **kwargs)
