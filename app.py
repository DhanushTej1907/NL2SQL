"""Streamlit demo for the NL2SQL compiler.

A thin view over :class:`nl2sql.pipeline.CompileResult`: it renders what the
compiler produced and holds no query logic of its own.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nl2sql import ir  # noqa: E402
from nl2sql.lexer import load_nlp, model_info  # noqa: E402
from nl2sql.pipeline import compile as compile_question  # noqa: E402
from nl2sql.schema import Database, load_database  # noqa: E402

SAMPLES_DIR = Path(__file__).resolve().parent / "samples"
UPLOAD_TYPES = ["db", "sqlite", "sqlite3", "sql"]
DIALECTS = ["sqlite", "postgres", "mysql", "duckdb", "bigquery"]

st.set_page_config(page_title="NL2SQL Compiler", layout="wide")


@st.cache_resource(show_spinner="Loading the language model...")
def _nlp():
    load_nlp()
    return model_info()


@st.cache_resource(show_spinner="Introspecting the database...")
def _load(path: str, _fingerprint: str) -> Database:
    return load_database(path)


def _fingerprint_of(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(str(path.resolve()).encode("utf-8"))
    try:
        stat = path.stat()
        digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8"))
    except OSError:
        pass
    return digest.hexdigest()


def discover_samples() -> list[Path]:
    if not SAMPLES_DIR.exists():
        return []
    found: list[Path] = []
    for pattern in ("*.db", "*.sqlite", "*.sqlite3", "*.sql"):
        found.extend(sorted(SAMPLES_DIR.glob(pattern)))
    return found


def sidebar() -> tuple[Path | None, str]:
    st.sidebar.header("Database")
    samples = discover_samples()
    labels = [p.name for p in samples]
    choice = st.sidebar.selectbox(
        "Bundled sample", labels, index=0 if labels else None,
        placeholder="no samples found",
    )
    uploaded = st.sidebar.file_uploader(
        "...or upload your own", type=UPLOAD_TYPES,
        help="A SQLite file, or a .sql dump which is transpiled to SQLite.",
    )
    dialect = st.sidebar.selectbox("SQL dialect to display", DIALECTS, index=0)

    if uploaded is not None:
        temp_dir = Path(tempfile.gettempdir()) / "nl2sql_uploads"
        temp_dir.mkdir(parents=True, exist_ok=True)
        target = temp_dir / uploaded.name
        target.write_bytes(uploaded.getbuffer())
        st.sidebar.success(f"Using the uploaded {uploaded.name}")
        return target, dialect

    if choice:
        return SAMPLES_DIR / choice, dialect
    return None, dialect


def show_schema(db: Database) -> None:
    with st.expander(f"Schema of {db.name} "
                     f"({len(db.symbols.tables)} tables)", expanded=False):
        if db.load_diagnostics:
            st.warning("Dump load notes:\n\n" + "\n\n".join(db.load_diagnostics[:10]))
        for table in db.symbols.tables.values():
            st.markdown(f"**{table.name}** — {table.row_count:,} rows")
            rows = []
            for col in table.columns.values():
                badges = []
                if col.is_pk:
                    badges.append("PK")
                if not col.nullable:
                    badges.append("NOT NULL")
                if col.unique:
                    badges.append("UNIQUE")
                rows.append(
                    {
                        "column": col.name,
                        "type": col.type_raw or col.type_class,
                        "class": col.type_class,
                        "constraints": ", ".join(badges),
                        "sample values": ", ".join(str(s) for s in col.samples[:4]),
                    }
                )
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            for fk in table.foreign_keys:
                icon = "declared" if fk.status == "declared" else fk.status
                st.caption(f"foreign key: {fk.describe()}  [{icon}]")


def badges(result) -> None:
    left, middle, right = st.columns(3)
    source = result.badge()
    if result.source == "compiler":
        left.success(f"Source: {source}")
    elif result.status != "ok":
        left.error(f"Source: {source}")
    else:
        left.warning(f"Source: {source}")

    eq = result.equivalence
    if eq is None:
        middle.info("Equivalence: not run")
    elif eq.mode == "n/a":
        middle.info(f"Equivalence: {eq.badge()}")
    elif eq.passed:
        middle.success(f"Equivalence: {eq.render()}")
    else:
        middle.error(f"Equivalence: {eq.render()}")

    total = sum(result.timings.values())
    rules = ", ".join(result.fired_rules()) or "none"
    right.info(f"Total {total:.0f} ms  ·  rules fired: {rules}")


def tab_tags(result) -> None:
    if not result.tags:
        st.info("The question was not tagged.")
        return
    tokens = result.lex.tokens if result.lex else []
    st.caption("Tokens: " + "  ".join(f"`{t.text}`" for t in tokens))
    rows = [
        {
            "tokens": " ".join(t.text for t in tokens[tag.start : tag.end]),
            "kind": tag.kind,
            "resolves to": tag.label(),
            "confidence": round(tag.confidence, 3),
            "how": tag.how,
        }
        for tag in result.tags.tags
    ]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    if result.tags.unmatched:
        st.warning(
            "Not matched to the schema: "
            + ", ".join(t.text for t in result.tags.unmatched)
        )


def tab_trace(result) -> None:
    if not result.trace:
        st.info("No rewrite rule fired for this question.")
    for entry in result.trace:
        st.markdown(f"### {entry.rule}")
        st.caption(entry.note)
        for check in entry.checks:
            (st.success if check.passed else st.error)(check.render())
        before, after = st.columns(2)
        before.caption("before")
        before.code(entry.before_sql, language="sql")
        after.caption("after")
        after.code(entry.after_sql, language="sql")
    if result.skipped_rules:
        st.markdown("### Rules that matched but did not fire")
        for entry in result.skipped_rules:
            with st.expander(f"{entry.rule} — {entry.reason()}"):
                for check in entry.checks:
                    (st.success if check.passed else st.error)(check.render())


def main() -> None:
    st.title("NL2SQL Compiler")
    st.caption(
        "A schema-agnostic natural-language-to-SQL compiler with an "
        "intent-aware, sound-by-construction optimizer."
    )
    model_name, vectors = _nlp()
    path, dialect = sidebar()
    st.sidebar.caption(
        f"spaCy model: {model_name} "
        + ("(word vectors available)" if vectors else "(no vectors)")
    )
    use_llm = st.sidebar.checkbox("Allow the LLM fallback", value=True)

    if path is None or not path.exists():
        st.info("Pick a bundled sample or upload a database to begin.")
        return

    try:
        db = _load(str(path), _fingerprint_of(path))
    except Exception as exc:
        st.error(f"Could not load {path.name}: {exc}")
        return

    show_schema(db)

    question = st.text_input(
        "Ask a question",
        placeholder="e.g. which departments have students with a high score",
    )
    if not question.strip():
        st.stop()

    with st.spinner("Compiling..."):
        try:
            result = compile_question(question, db, dialect=dialect, use_llm=use_llm)
        except Exception as exc:
            st.exception(exc)
            return

    badges(result)

    for diag in result.diagnostics:
        if diag.level == "error":
            st.error(diag.render())
        elif diag.level == "warning":
            st.warning(diag.render())

    tabs = st.tabs(
        ["Results", "Tokens & tags", "AST", "IR", "Optimized IR", "SQL",
         "Rewrite trace", "Plan before/after", "Timings", "Diagnostics"]
    )

    with tabs[0]:
        if result.rows:
            st.dataframe(
                pd.DataFrame(result.rows, columns=result.columns or None),
                width="stretch",
            )
            st.caption(f"{len(result.rows)} rows (capped at 1000)")
        elif result.ok:
            st.info("The query ran and returned no rows.")
        else:
            st.error("No results: the question did not compile.")

    with tabs[1]:
        tab_tags(result)

    with tabs[2]:
        st.code(result.ast.pretty() if result.ast else "no AST", language="text")

    with tabs[3]:
        st.code(result.plan.pretty() if result.plan else "no IR", language="text")

    with tabs[4]:
        st.code(
            result.optimized_plan.pretty() if result.optimized_plan else "no IR",
            language="text",
        )

    with tabs[5]:
        left, right = st.columns(2)
        left.markdown("**Naive SQL** (a literal translation of the wording)")
        left.code(result.naive_sql or "-", language="sql")
        right.markdown(f"**Optimized SQL** ({dialect})")
        right.code(result.dialect_sql or result.optimized_sql or "-", language="sql")

    with tabs[6]:
        tab_trace(result)

    with tabs[7]:
        left, right = st.columns(2)
        left.markdown("**EXPLAIN QUERY PLAN — before**")
        left.code("\n".join(result.plan_before) or "-", language="text")
        right.markdown("**EXPLAIN QUERY PLAN — after**")
        right.code("\n".join(result.plan_after) or "-", language="text")

    with tabs[8]:
        st.dataframe(
            pd.DataFrame(
                [{"stage": k, "milliseconds": round(v, 3)}
                 for k, v in result.timings.items()]
            ),
            width="stretch",
            hide_index=True,
        )

    with tabs[9]:
        if not result.diagnostics:
            st.success("No diagnostics.")
        for diag in result.diagnostics:
            st.write(diag.render())


main()
