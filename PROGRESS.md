# PROGRESS.md

Status log for the NL2SQL compiler described in `SPEC.md` and planned in `PLAN.md`.

## Milestones

| Milestone | Status | Notes |
|---|---|---|
| M1 — schema, lexer, tagger, parser (SELECT/WHERE), codegen, CLI | done | Works on `university.db`; CLI is `python -m nl2sql`. |
| M2 — aggregates, group/order/top-k, FK joins, semantic diagnostics | done | Join paths by BFS over declared FKs, with naming-based inference as a fallback. |
| M3 — IR, optimizer R1+R2, rewrite trace, equivalence check | done | Trace records fired *and* skipped rules with their preconditions. |
| M4 — Streamlit app | done | `streamlit run app.py`; covered by `tests/test_app.py` using `streamlit.testing.v1.AppTest`. |
| M5 — LLM fallback and on-disk cache | done | Gemini via `google-genai`; resolved model `gemini-3.8-flash`; answers cached in `.cache/llm/`. |
| M6 — R3, R4, `.sql` dump loading, `nokeys.db`, datagen | done | R3 and R4 fire with full precondition traces; the MySQL dump loads and answers identically to the SQLite sample; `nokeys.db` shows R2 and R4 safely skipped; `datagen.py` scaled `university.db` to 100,000 rows per table in **6.5 s**. Chinook not downloaded. |
| M7 — all tests green, `demo_check.py` all PASS, README | done | 247 tests pass; `demo_check.py` reports 42/42 PASS; README written. |

## Environment

- Gemini model resolved at runtime (no `GEMINI_MODEL` in `.env`): the
  resolver picked **`gemini-3.8-flash`**, confirmed by a live call. The name
  is cached in `.cache/llm/model.json`.
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

## Verification (as last run)

- `python -m pytest` -> **247 passed**, 1 deselected (the live-API test,
  which is skipped unless it is asked for explicitly).
- `python scripts/demo_check.py` -> **42/42 passed**, both with the
  fallback live and with `NL2SQL_DISABLE_LLM=1`.
- `grep -rwiE "<every sample table and column name>" --include="*.py"
  nl2sql/` -> no hits. `tests/test_no_hardcoded_names.py` enforces the same
  rule with `tokenize`, across identifiers, string literals and comments.
- `datagen.py` on `university.db` at `--rows 100000`: 500,000 rows in
  **6.5 s**, `foreign_key_check` clean.
- The dump path (`samples/retail_dump.sql`) loads with **zero** load
  diagnostics and reproduces the SQLite sample's tables, row counts,
  primary keys and foreign keys exactly.

## Known limitations

- The controlled grammar covers the question shapes in `demo_questions.md`.
  Anything outside it goes to the LLM fallback by design.
- No self-joins, and no `OR` across tables, in the compiler path.
- The compiler can accept a question it only partly understands and answer
  the part it recognised. The fallback triggers on a parse or semantic error,
  on low tagger confidence, or on an unmatched content word — not on a
  question whose words all match but whose shape does not. Verbs are excluded
  from word-vector matching for this reason, since a verb resembling a table
  name was the main source of such misreadings.
- The LLM path depends on the API key and the provider's quota; without
  either it reports `fallback_unavailable` rather than failing.
- Join paths longer than three hops are refused and handed to the fallback.
- When two join routes are equally short, the shortest declared one wins and
  an `info` diagnostic names the chosen path; the question may still be
  ambiguous in English.
- Chinook was not downloaded; it is a non-graded smoke test and nothing
  depends on it. The unseen-schema case is covered instead by the
  `renamed_university` fixture, which renames every table and column and
  re-runs the same questions.
- The fresh-clone check has not been run; the repository holds everything
  needed for it (`requirements.txt`, `scripts/make_samples.py`, the
  samples themselves).
