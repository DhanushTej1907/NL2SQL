# PROGRESS.md

Status log for the NL2SQL compiler described in `SPEC.md` and planned in `PLAN.md`.

## Milestones

| Milestone | Status | Notes |
|---|---|---|
| M1 — schema, lexer, tagger, parser (SELECT/WHERE), codegen, CLI | done | Works on `university.db`; CLI is `python -m nl2sql`. |
| M2 — aggregates, group/order/top-k, FK joins, semantic diagnostics | done | Join paths by BFS over declared FKs, with naming-based inference as a fallback. |
| M3 — IR, optimizer R1+R2, rewrite trace, equivalence check | done | Trace records fired *and* skipped rules with their preconditions. |
| M4 — Streamlit app | done | `streamlit run app.py`; covered by `tests/test_app.py` using `streamlit.testing.v1.AppTest`. |
| M5 — LLM fallback and on-disk cache | pending | |
| M6 — R3, R4, `.sql` dump loading, `nokeys.db`, datagen | partly done | R3 and R4 are implemented and firing; dump loading is implemented in `schema.py`; `nokeys.db` is built. datagen pending. |
| M7 — all tests green, `demo_check.py` all PASS, README | pending | |

## Environment

- Python 3.12.5 in `C:\NL2SQL\.venv` (3.14 is the machine default, but spaCy's
  compiled dependencies have reliable Windows wheels on 3.12).
- spaCy model: `en_core_web_md` installed successfully, so word vectors are
  available and vector matching is active.
- Sample databases are rebuilt deterministically with
  `python scripts/make_samples.py` (seeded).

## Choices made

- **Naive lowering is deliberately literal.** Every joined (non-target) table
  is lowered as a derived table projecting all of its columns, and an
  existence question is lowered as `SELECT COUNT(*) > 0`. That is a faithful
  translation of the wording rather than a good plan, which is what gives the
  optimizer honest work to do. The optimized SQL, not the naive SQL, is the
  one the demo runs.
- **Trust model.** A precondition may only rely on a declared PRIMARY KEY,
  NOT NULL, UNIQUE, or a declared FOREIGN KEY that `PRAGMA foreign_key_check`
  reports as clean. A declared FK whose data is broken is demoted to
  `declared_violated` and treated exactly like an inferred edge.
- **SQLite NULL quirk.** A non-`INTEGER PRIMARY KEY` column can hold NULL in
  SQLite, so any rule needing key uniqueness also requires the key columns to
  be NOT NULL or a rowid alias.
- **R2 is stricter than the spec.** `DISTINCT` is dropped only when the
  projection contains the outer table's full, non-null primary key. Otherwise
  the rewrite still removes the join fan-out but keeps `DISTINCT`.
- **R4 is slightly broader than the spec.** References to the referenced key
  column above the join are rewritten to the foreign-key column, which is
  sound under the same preconditions. R4 additionally refuses to fire when the
  joined table carries a filter of its own, since such a join can remove rows.
- **R3 only prunes subquery projections**, never the projection the user sees.
- **Determinism.** Ordered queries get the base table's primary key appended
  as a tiebreaker in both the naive and the optimized SQL, so `LIMIT` results
  and the ordered equivalence comparison are stable.
- **Identifiers are always quoted** in generated SQL.
- **Safety.** Every query runs on a read-only connection with a wall-clock
  guard, after a sqlglot check that it is a single `SELECT`.
- Extra files beyond the spec layout: `scripts/make_samples.py` (sample
  builder) and `nl2sql/sql_to_ir.py` (LLM SQL to IR helper, M5).

## Known limitations

- The controlled grammar covers the question shapes in `demo_questions.md`.
  Anything outside it goes to the LLM fallback by design.
- No self-joins, and no `OR` across tables, in the compiler path.
- Join paths longer than three hops are refused and handed to the fallback.
- When two join routes are equally short, the shortest declared one wins and
  an `info` diagnostic names the chosen path; the question may still be
  ambiguous in English.
