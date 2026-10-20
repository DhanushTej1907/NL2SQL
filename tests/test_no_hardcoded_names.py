"""No sample table or column name may appear anywhere in ``nl2sql/``.

A *specific* name (one that is not ordinary English or SQL vocabulary) must
not appear at all: not as an identifier, not in a string, not in a comment.

A *generic* name is a word like "name" or "total" that is also English
grammar, a SQL keyword, or a key in a PRAGMA or driver result. Prose may use
such a word freely, so it is rejected only as a bare one-word string
literal, and then only when it is neither registered as a grammar word in
``tagger.GRAMMAR_WORDS`` nor listed in ``SQL_VOCAB``. That is exactly where
a hardcoded column name would hide.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest

from conftest import ROOT, SAMPLES
from nl2sql.schema import introspect, pluralize_forms
from nl2sql.tagger import GRAMMAR_WORDS

PACKAGE = ROOT / "nl2sql"

# The specification defines the inferred-key convention as "<stem>_id -> id",
# so this one literal is allowed.
ALLOWED = {"id"}

# Words that are ordinary English or Python vocabulary as well as column
# names in one of the samples.
GENERIC = {
    "name", "names", "table", "tables", "column", "columns", "value",
    "values", "label", "text", "status", "year", "total", "count", "sum",
    "min", "max", "index", "key", "keys", "type", "kind", "items", "item",
    "price", "date", "city", "country", "email", "stock", "score", "title",
    "building", "budget", "grade", "credits", "quantity", "salary",
    "designation", "semester", "opponent", "points", "weight",
    # also ordinary English or Python vocabulary, and column names in the
    # downloaded Chinook sample
    "bytes", "state", "address", "phone", "company", "composer", "position",
    "line", "lines", "support", "unit", "units", "track", "media", "invoice",
}

# SQL type words, PRAGMA and driver result keys, and sqlglot argument names
# the modules legitimately write as strings.
SQL_VOCAB = {
    "name", "type", "table", "tables", "column", "columns", "key", "keys",
    "index", "value", "values", "text", "date", "time", "year", "integer",
    "real", "unique", "default", "null", "constraint", "detail", "schema",
    "kind", "label", "items", "item", "status", "this", "expressions",
}

ALLOWED_LITERALS = set(GRAMMAR_WORDS) | SQL_VOCAB | ALLOWED

_QUOTES = ('"""', "'''", '"', "'")


def sample_names() -> set[str]:
    names: set[str] = set()
    for path in sorted(SAMPLES.glob("*.db")) + sorted(SAMPLES.glob("*.sqlite")):
        symbols = introspect(path)
        for table in symbols.tables.values():
            names.add(table.name.lower())
            for column in table.columns:
                names.add(column.lower())
    return {n for n in names if n not in ALLOWED}


def python_files() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _tokens(path: Path):
    source = path.read_text(encoding="utf-8")
    return list(tokenize.generate_tokens(io.StringIO(source).readline))


def strings_and_comments(path: Path) -> str:
    return "\n".join(
        t.string for t in _tokens(path)
        if t.type in (tokenize.STRING, tokenize.COMMENT)
    ).lower()


def bare_literals(path: Path) -> set[str]:
    """String literals whose whole content is a single word."""
    out: set[str] = set()
    for token in _tokens(path):
        if token.type != tokenize.STRING:
            continue
        body = token.string.strip()
        for quote in _QUOTES:
            if body.startswith(quote) and body.endswith(quote) and len(body) > 1:
                body = body[len(quote) : -len(quote)]
                break
        body = body.strip().lower()
        if body and " " not in body:
            out.add(body)
    return out


def code_identifiers(path: Path) -> set[str]:
    return {t.string.lower() for t in _tokens(path) if t.type == tokenize.NAME}


def test_the_samples_actually_provide_names():
    assert len(sample_names()) > 20, "the check needs real names to look for"


@pytest.mark.parametrize("path", python_files(), ids=lambda p: p.name)
def test_no_sample_name_is_hardcoded(path):
    names = sample_names()
    specific = names - GENERIC
    # A plural is just as much a leak as the singular form.
    specific |= {p for n in specific for p in pluralize_forms(n)} - GENERIC
    generic_here = names & GENERIC

    found: list[str] = []

    identifiers = code_identifiers(path)
    for name in sorted(specific):
        if name in identifiers:
            found.append(f"identifier {name!r}")

    text = strings_and_comments(path)
    for name in sorted(specific):
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text):
            found.append(f"{name!r} in a string or comment")

    literals = bare_literals(path)
    for name in sorted(generic_here):
        if name not in ALLOWED_LITERALS and name in literals:
            found.append(f"{name!r} as a bare string literal")

    assert not found, f"{path.name} mentions schema names: {sorted(set(found))}"


def test_specific_names_exist_to_be_checked():
    """The specific set must be non-trivial, or the check proves nothing."""
    specific = sample_names() - GENERIC
    assert len(specific) >= 10, sorted(specific)


def test_the_check_would_catch_a_real_leak(tmp_path):
    """A guard on the guard: a planted name must be visible to the scan."""
    leaky = tmp_path / "leaky.py"
    leaky.write_text('TABLE = "enrollment"\nx = enrollment\n', encoding="utf-8")
    assert "enrollment" in strings_and_comments(leaky)
    assert "enrollment" in code_identifiers(leaky)
    assert "enrollment" in sample_names() - GENERIC


def test_a_planted_generic_literal_is_caught(tmp_path):
    leaky = tmp_path / "leaky.py"
    leaky.write_text('COLUMN = "designation"\n', encoding="utf-8")
    assert "designation" in bare_literals(leaky)
    assert "designation" not in ALLOWED_LITERALS
