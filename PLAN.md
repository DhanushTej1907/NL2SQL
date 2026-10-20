# PLAN.md — NL2SQL Compiler (Review 2)

Implementation plan for the build described in `SPEC.md`. Nothing has been installed or written yet; this file is the plan only.

---

## 0. Toolchain decisions

| Item | Choice | Why |
|---|---|---|
| **Python** | **3.12** (`py -3.12`, already installed at `...\Programs\Python\Python312`) | 3.14 is the machine default, but spaCy and its compiled dependencies (thinc, blis, cymem, preshed) have the most reliable prebuilt Windows wheels on 3.12. 3.12 meets the spec's "3.10+" requirement and avoids compiling C extensions on Windows. |
| Virtualenv | `C:\NL2SQL\.venv`, created with `py -3.12 -m venv .venv` | Always invoked as `.venv\Scripts\python.exe ...`, so PowerShell execution policy (`Activate.ps1`) can't break anything. |
| **Gemini model** | **Resolved at runtime; expected to be the newest stable Flash model, e.g. `gemini-2.5-flash` or a newer stable `gemini-*-flash` if the key lists one.** | `.env` sets no `GEMINI_MODEL`, so the spec says to list models and pick a current Flash. The resolver: (1) uses `GEMINI_MODEL` if set; (2) otherwise calls `client.models.list()` and keeps models that support `generateContent`, have `flash` in the name, and do **not** contain `lite`, `preview`, `exp`, `tts`, `image`, `live` or `audio`; (3) picks the highest version number; (4) only if every stable candidate is filtered out, allows a `preview` Flash. The resolved name is cached in `.cache/llm/model.json` and written to `PROGRESS.md` at M5. I'm not calling the API now (no installs yet), so the exact name is confirmed in M5. |
| spaCy model | `en_core_web_md` (vectors), falling back to `en_core_web_sm` | Installed with `python -m spacy download`, not through `requirements.txt`, because the model wheel version must match the installed spaCy. |
| Libraries | spacy 3.8.x, sqlglot, SQLAlchemy 2.0.x, streamlit 1.x, google-genai 1.x, python-dotenv, Faker, numpy, pytest | After the first working install, `requirements.txt` gets exact pins (`==`), because sqlglot's AST API changes between minor versions. |
| Git | `git init` at the start of M1, one commit per milestone | `.gitignore` is written **before the first commit** and covers `.env`, `.venv/`, `.cache/`, `__pycache__/`, temporary DBs and `*.scaled.db`. Commit messages end with the required Co-Authored-By line. If no git identity is configured, I set a repo-local one from the account email and note it in PROGRESS.md. |

---

## 1. Architecture

```
question ─► lexer ─► tagger ─► parser ─► semantic ─► lower to IR ─► optimizer ─► codegen ─► execute
              │         │         │          │            │              │            │          │
           tokens     tags       AST    diagnostics   naive IR      optimized IR   naive/opt   rows, plans,
                                        + join paths                 + trace        SQL       equivalence
                 ▲                                    │
                 └──── low confidence / parse or semantic error ──► llm_fallback ──► sqlglot ─► IR (subset) ─► same back end
```

Cross-cutting rules that every milestone follows:

1. **No schema names in `nl2sql/`.** Every table and column name comes from the `SymbolTable`. English function words ("how many", "per", "top", "more than") may appear in code. They are grammar, not schema. Docstrings and comments use invented names (`widget`, `zorb_id`) so the hardcoded-name grep stays clean. `"id"` is the one exception: the spec's naming convention for inferred keys (`x_id -> x.id`) needs it.
2. **Trust model for constraints.** A rule precondition may rely on: declared PRIMARY KEY, declared NOT NULL, declared UNIQUE, and declared FOREIGN KEY **only if `PRAGMA foreign_key_check` reports no violations for that FK at load time**. SQLite doesn't enforce FKs by default, so a declared FK with dangling rows is downgraded to `declared_violated` and treated like an inferred edge. Inferred edges never satisfy a precondition.
3. **SQLite quirk.** A non-`INTEGER PRIMARY KEY` column can hold NULL in SQLite. Where a rule needs PK uniqueness, it also requires the PK columns to be NOT NULL or a rowid alias.
4. **Safety.** Every query (compiler or LLM) runs on a read-only connection (`file:...?mode=ro`, `uri=True`), with a progress-handler timeout, after a sqlglot check that it is a single `SELECT`.
5. **Determinism.** For compiler-generated SQL with ORDER BY, codegen appends the base table's PK as a tiebreaker in **both** the naive and the optimized SQL. That makes LIMIT results and the ordered equivalence comparison deterministic.
6. **Identifiers are always quoted** in generated SQL (handles reserved words, spaces and mixed case, as in Chinook).
7. **Dialects.** Codegen can emit any sqlglot dialect for display (`--dialect postgres`), but execution, plans and equivalence always use the SQLite rendering.

---

## 2. Milestones

### M1 — Vertical slice: schema → lexer → tagger → parser (SELECT/WHERE) → codegen → CLI, on `university.db`

**Files**
- `requirements.txt`, `.gitignore`, `PROGRESS.md` (skeleton)
- `scripts/make_samples.py`: deterministic (seeded) builder for every sample DB. In M1 it builds only `university.db`.
- `samples/university.db`
- `nl2sql/__init__.py`, `nl2sql/schema.py`, `nl2sql/lexer.py`, `nl2sql/tagger.py`, `nl2sql/parser.py`, `nl2sql/codegen.py` (direct AST→SQL for now), `nl2sql/pipeline.py` (minimal), `nl2sql/__main__.py`
- `tests/conftest.py`, `tests/test_schema.py`, `tests/test_lexer.py`, `tests/test_tagger.py`, `tests/test_m1_select_where.py`

**Key design decisions**
- **schema.py.** `load_database(path) -> Database` holds the path, the `SymbolTable` and a read-only connection factory. Introspection goes through the SQLAlchemy `inspect()` API (tables, columns, types, PK, FKs, nullable, unique constraints), cross-checked with `PRAGMA table_info` / `foreign_key_list`. Each column gets a type class (`numeric`, `text`, `date`, `other`) from SQLite type affinity rules. Up to 50 sampled distinct values per text column (`SELECT DISTINCT ... WHERE col IS NOT NULL LIMIT 50`). The schema hash is SHA-256 of canonical JSON of tables, columns, types and keys. It ignores data, so reloading the same schema hits the LLM cache.
- **lexer.py.** spaCy is loaded once (module-level singleton, `md` falling back to `sm`, recording `has_vectors`). A pre-pass rewrites multi-word comparison phrases into single operator tokens (`more than / greater than / above / over / exceeding → >`, `at least / no less than → >=`, `below / under / less than / fewer than → <`, `at most / no more than → <=`, `equal to / equals / is → =` in comparison context, `not / other than → !=`). Longest match wins, so "no more than" beats "more than". Each token carries `text, lower, lemma, pos, is_num, num_value, is_quoted, span`. Number words ("five") are converted to numbers. Quoted strings stay as one value token.
- **tagger.py.** A lexicon built from the SymbolTable: each table and column produces name variants by splitting snake_case, camelCase and PascalCase, plus lowercase, lemma, singular and plural forms, and the joined form ("order items" ↔ `order_items`). Matching tries n-gram spans (up to 3 tokens, longest first). Confidences: exact or lemma match = 1.0 / 0.95; abbreviation (prefix or subsequence match such as `dept`↔`department`, accepted only if vector similarity agrees or the match is unique) = 0.8; word-vector cosine above 0.70 = the similarity score (disabled when the model has no vectors); value hit against sampled values = 0.9. Stopwords and grammar keywords are never tagged as schema or values, so the article "a" can't match a grade value `A`. Single-letter values need quotes or uppercase. A column name shared by several tables keeps every candidate; semantic analysis resolves it. For capitalized or quoted tokens missing from the samples, one bounded exact-match lookup (`SELECT 1 ... WHERE col = ? LIMIT 1`) runs per text column.
- **parser.py.** A hand-written recursive-descent parser over the tag stream (not a general CFG), with a small documented controlled grammar:
  `Query := [Command] [Quantifier] Target [Projection] [Filter*] [Group] [Order] [Limit]`.
  M1 handles `show/list/display/find/get/what are/which` + a table, an optional column list ("names and gpa of students"), and filters `<col> <op> <value>` / `<value>` (a value tag implies its own column) joined by `and`. AST nodes are dataclasses: `Query, ColumnRef, Condition, Aggregate, OrderSpec, ...`.
- **Default projection.** When the user names only a table, every column is listed explicitly (never `*`), so the column-pruning rule has something to work on later.
- **CLI.** `python -m nl2sql "question" --db PATH [--dialect sqlite] [--show tokens,tags,ast,ir,trace,plan] [--json] [--no-llm]`. stdout is reconfigured to UTF-8 so Windows consoles don't crash.

**Verification**
- `pytest tests/test_schema.py tests/test_lexer.py tests/test_tagger.py tests/test_m1_select_where.py`
- CLI smoke runs print SQL and rows:
  `python -m nl2sql "show students with gpa above 3.5" --db samples/university.db`,
  `"list names of students in CSE"` (value lookup → department name),
  `"show courses with credits at least 4"`.
- A **schema-agnostic test** (added now, extended every milestone): a fixture copies `university.db` with every table and column renamed (`student → learner`, `gpa → score_avg`, ...) and checks the same questions, rephrased, still compile correctly.
- Commit `M1`.

**Risks**
- spaCy install on Windows. Mitigation: Python 3.12 wheels; if `md` won't download, use `sm` and record it.
- Tagger false positives on common words (e.g. a table called `orders` vs "in order"). Mitigation: grammar keywords are consumed before schema matching, and conflicts are covered by tests.
- Lemmatizer quirks ("data", "faculty" as mass nouns). Mitigation: match both lemma and raw lowercase forms.

---

### M2 — Aggregates, group/order/top-k, FK joins, semantic diagnostics

**Files**
- `nl2sql/semantic.py`, with `parser.py`, `tagger.py` and `codegen.py` extended
- `scripts/make_samples.py` gains `retail.db`
- `tests/test_parser_aggregates.py`, `tests/test_semantic.py`, `tests/test_joins.py`

**Key design decisions**
- **Aggregates.** "how many / number of / count" → COUNT; "average / mean" → AVG; "total / sum" → SUM; "minimum / lowest / smallest" and "maximum / highest / largest" → MIN/MAX **only** when the question asks for a value ("what is the highest gpa"). When it asks for entities ("which student has the highest gpa", "top 5 products by price"), the parser emits ORDER BY ... DESC/ASC with LIMIT k (k = 1 if not given). This is the compile-time top-k choice from the spec, tagged `top_k`.
- **Group by.** "per / for each / by / in each <X>". If X is a column, group by it. If X is a table, group by that table's key (through the FK join), and also project the table's **label column**, chosen schema-agnostically: the first non-key text column with the highest distinct ratio among sampled values (no name guessing).
- **Order.** "sorted/ordered by <col> [ascending|descending]", "highest/lowest first".
- **semantic.py**
  - Resolves ambiguous column refs: prefer the target table, then tables already in the join set, then the shortest join path.
  - Builds the FK graph: declared edges (`declared`, or `declared_violated` per §1.2) plus inferred edges when none are declared on a table pair (`x_id` / `xId` / `XId` → table `x`, `xs`, `xes` or `x_s`, column `id` or `x_id` or the table's PK), marked `inferred`.
  - BFS over the undirected graph for the minimal set of edges connecting every referenced table (Steiner-style: BFS from the target, greedily adding the nearest needed table), preferring declared edges on ties. Tested for 1- and 2-hop paths. Paths longer than 3 hops are refused and sent to the LLM.
  - Type checks: numeric comparator on a text column → error; non-numeric literal against a numeric column → error; aggregate other than COUNT on a text column → error.
  - Diagnostics are `Diagnostic(level, code, message, span, suggestions)`, with "did you mean" suggestions from `difflib.get_close_matches` over all table names, column names and sampled values for any unmatched content noun.
- In M2 codegen still renders from the AST. M3 swaps it to the IR without changing output.

**Verification**
- Tests for COUNT/AVG/SUM/MIN/MAX, "how many", GROUP BY via "per / for each / by", top-k, 1-hop joins (student→department) and 2-hop joins (student→enrollment→course), each checked by executing the SQL and comparing against a hand-written reference query's result.
- Diagnostics tests: "students with name above 5" → type error; "show studnets" → suggestion `student`.
- The same aggregate/join questions run on `retail.db` (plural tables, `customer_id`-style keys) **with no code changes**, which is part of the schema-agnostic check.
- Commit `M2`.

**Risks**
- Superlative ambiguity (MAX vs top-1). Mitigation: a documented rule plus tests; the rare case that doesn't fit goes to the LLM.
- Join-path ambiguity when multiple FK routes exist (e.g. course→department directly vs course→faculty→department). Mitigation: shortest path wins, declared beats inferred, and an `info` diagnostic names the chosen path.

---

### M3 — IR + optimizer R1, R2 with trace + equivalence

**Files**
- `nl2sql/ir.py`, `nl2sql/optimizer.py`, `nl2sql/equivalence.py`; `codegen.py` now renders IR via sqlglot expressions; `pipeline.py` now returns the full `CompileResult`
- `tests/test_ir.py`, `tests/test_optimizer_r1.py`, `tests/test_optimizer_r2.py`, `tests/test_equivalence.py`

**Key design decisions**
- **IR nodes** (frozen dataclasses): `Scan, Select, Project(distinct), Join(kind, on, edge), SemiJoin, AntiJoin, Aggregate(group_by, aggs), Sort, Limit, Exists`. Expressions: `ColRef, Literal, Cmp, And, Or, Not, AggCall`. Each `Join` carries the FK `edge` it came from (with `declared / declared_violated / inferred`), so rules can check provenance. Intent tags (`existence, semi_join, top_k, distinct`) live on the root. `ir.pretty()` gives an indented tree for the UI and CLI.
- **Lowering (AST → naive IR)** is a literal translation of the wording, which gives the optimizer honest work:
  - "is there / are there any ..." → `Project[COUNT(*) > 0](Aggregate(...))`, tag `existence`
  - "which X have <Y-condition>" → `Project[distinct, X cols](Select(Join(X, Y)))`, tag `semi_join`
- **Optimizer framework.** Each rule has `name`, `matches(node)`, `preconditions(node, ctx) -> list[Check(description, passed, evidence)]`, and `apply(node)`. The engine traverses bottom-up and runs to a fixpoint (capped at 10 passes). On a firing, the trace gets `{rule, before_sql, after_sql, preconditions}`, where the SQL is the whole query rendered before and after. When a rule matches but a precondition fails, a `skipped` entry records which check failed. That's how `nokeys.db` shows rules safely not firing.
- **R1 existence.** `COUNT(*) > 0` / `COUNT(*) >= 1` / `COUNT(*) <> 0` at the top level → `SELECT EXISTS(SELECT 1 FROM ... WHERE ...)`. Structural preconditions: no GROUP BY / HAVING on that aggregate, and the argument is `*` (a `COUNT(col)` variant would need `col IS NOT NULL` added; it's supported and traced as such). No schema constraints needed. In SQLite both forms return 0/1.
- **R2 semi-join.** `DISTINCT outer_cols FROM outer JOIN inner ON fk WHERE p` → `outer_cols FROM outer WHERE EXISTS (SELECT 1 FROM inner WHERE fk_match AND p)`. Preconditions: (a) only outer-table columns are projected (and none used by ORDER BY above); (b) the join edge is a declared, non-violated FK; (c) the outer table has a declared PK. **Stricter than the spec for soundness:** DISTINCT is dropped only when (d) the projection includes the full outer PK and those columns are NOT NULL. Otherwise R2 keeps `DISTINCT` on the rewritten query. The rewrite is still a win (no join fan-out) and remains equivalent (two outer rows with identical projected values would otherwise wrongly survive). The trace records which variant fired.
- **equivalence.py.** Executes both SQL strings with a timeout. Results are compared as multisets (`Counter` over rows, with floats rounded to 9 significant digits) unless the outermost statement has ORDER BY, in which case rows are compared as lists (deterministic thanks to the PK tiebreaker, §1.5). Returns `EquivalenceResult(passed, mode, n_rows_original, n_rows_optimized, first_difference)`.
- **pipeline.compile(question, db)** returns `CompileResult`: tokens, tags, ast, ir, optimized_ir, naive_sql, optimized_sql, dialect_sql, trace, skipped_rules, source, rows, columns, timings (per stage, `perf_counter`), plan_before, plan_after (`EXPLAIN QUERY PLAN`), equivalence, diagnostics.

**Verification**
- R1 and R2 tests on `university.db` and `retail.db`: the rule fires, the trace has the right preconditions, the optimized SQL matches the expected shape (compared via sqlglot AST, not strings), and equivalence passes.
- Negative tests: R2 with a non-PK projection keeps DISTINCT; R2 with an inner-table column projected doesn't fire; a fixture DB with a dangling FK value demotes the edge and R2 is skipped with a reason.
- `EXPLAIN QUERY PLAN` before/after is captured and non-empty.
- Commit `M3`.

**Risks**
- sqlglot's builder/AST API differs across versions. Mitigation: pinned version; all sqlglot use goes through a few helpers in `codegen.py`.
- The DISTINCT nuance in R2 (above) is the main soundness trap, handled by the stricter precondition.

---

### M4 — Streamlit app

**Files**
- `app.py`, `tests/test_app.py` (uses `streamlit.testing.v1.AppTest`)

**Key design decisions**
- Sidebar: a dropdown of bundled samples (auto-discovered from `samples/*.db|*.sqlite|*.sql`) plus a file uploader (`.sqlite`, `.db`, `.sql`). Uploads go to a per-session temp dir. The DB is cached with `st.cache_resource` keyed on the file's content hash. spaCy is cached once.
- Main area: introspected schema (one expander per table: columns, types, PK/FK/NOT NULL badges, FK edges with declared/inferred markers), the question box, then the **source badge** (green "Compiler" / amber "LLM fallback" / grey "LLM — unoptimized"), the **equivalence badge** (pass/fail), and timings.
- Tabs: Tokens & tags (table with confidences) · AST · IR · Optimized IR · SQL (naive | optimized side by side, plus the dialect selector) · Rewrite trace (fired + skipped, with precondition checkmarks) · Results (dataframe, capped at 1,000 rows) · Plan before/after · Diagnostics.
- The app is a thin view over `CompileResult`; it has no logic of its own.

**Verification**
- `AppTest` script: select `university.db`, enter a question, run it; assert the badge text, all tabs, no exception, and a non-empty results dataframe.
- Manual smoke: `streamlit run app.py --server.headless true`, then `curl http://localhost:8501/_stcore/health` returns `ok`, then shut it down.
- Commit `M4`.

**Risks**
- `AppTest` and tab APIs vary across Streamlit versions. Mitigation: pinned version, with assertions on markdown text rather than widget internals.
- Windows file locks on temp DBs. Mitigation: connections are closed promptly, and cleanup uses `ignore_errors`.

---

### M5 — LLM fallback + cache

**Files**
- `nl2sql/llm_fallback.py`, `nl2sql/sql_to_ir.py` (helper module for sqlglot AST → IR on the supported subset; kept separate to keep `llm_fallback.py` small)
- `tests/test_llm_fallback.py` (fake client, no network), `tests/test_sql_to_ir.py`

**Key design decisions**
- **Triggers**: a parse error, a semantic error, any schema tag the parser relied on below confidence 0.6, or an unconsumed content word (noun/adjective not matched to schema or grammar).
- **Prompt**: compact schema (tables, columns with types, PK, FK, NOT NULL, up to 5 sample values per text column), the question, and the instruction "return exactly one SQLite SELECT statement, no prose". `temperature=0`. The response is extracted from a code fence if present.
- **Validation**: `sqlglot.parse` must yield exactly one statement, and it must be a `Select` (or a set operation of Selects). Anything else is rejected with a diagnostic. Execution uses the read-only connection.
- **SQL → IR** supports: a single SELECT, FROM plus inner equi-joins, WHERE conjunctions/disjunctions of comparisons, IN-lists, BETWEEN, `EXISTS`/`IN` subqueries (→ SemiJoin), `NOT EXISTS` (→ AntiJoin), GROUP BY/HAVING, the five aggregates, DISTINCT, ORDER BY, LIMIT. Join edges are matched against the FK graph, so provenance is still known. If it converts: semantic checks + optimizer + equivalence as usual, `source="llm"`. If not (window functions, CTEs, outer joins, correlated scalar subqueries, UNION...): the SQL is kept verbatim, marked `unoptimized`, executed to validate, and equivalence is reported as "n/a — validated by execution".
- **Cache**: `.cache/llm/<sha256(schema_hash | normalized question | model)>.json` stores the raw response, SQL, model and timestamp. Writes are atomic (temp file + `os.replace`). Questions are normalized by lowercasing and collapsing whitespace.
- **Graceful degradation**: no key, `google-genai` import failure, network error, timeout, quota error, or empty response all produce `CompileResult(source="llm", status="fallback_unavailable", diagnostics=[...reason...])`. It never raises. `--no-llm` / `NL2SQL_DISABLE_LLM=1` forces this path, and tests use it.
- The key is read only via python-dotenv / `os.environ`, never logged, never put in the cache or `CompileResult`. (`.env` has a space before `=`; python-dotenv handles that. M5 checks the key loads, without printing it.)

**Verification**
- Unit tests with a fake client: prompt contains schema and question; cache miss → call → cache hit with no second call; an unsupported construct → `unoptimized` but executed; a `DROP TABLE` response is rejected; no key → `fallback_unavailable`; network exception → `fallback_unavailable`.
- One live smoke test, `@pytest.mark.live`, skipped unless the key is present and the network is reachable: asks one fallback question per sample DB and records the resolved model name in PROGRESS.md.
- Commit `M5`.

**Risks**
- Model listing API shape or model availability changes. Mitigation: defensive attribute access, and the resolution logic is unit-tested against a fake list.
- Quota or rate limits. Mitigation: the cache plus graceful degradation; demo_check treats "unavailable but handled" as PASS, per the spec.
- LLM hallucinated columns. Mitigation: sqlglot qualification against the SymbolTable before execution produces a diagnostic, not a crash.

---

### M6 — R3, R4, `.sql` dump support, `nokeys.db`, Chinook, datagen

**Files**
- `optimizer.py` gains R3 and R4; `schema.py` gains dump loading
- `samples/retail.db` (done in M2), `samples/retail_dump.sql`, `samples/nokeys.db`, `samples/chinook.db` (if the download works)
- `datagen.py`
- `tests/test_optimizer_r3.py`, `tests/test_optimizer_r4.py`, `tests/test_dump_loading.py`, `tests/test_nokeys.py`, `tests/test_datagen.py`, `tests/test_soundness_scaled.py`

**Key design decisions**
- **R3 column pruning.** Naive lowering renders filtered join inputs and EXISTS bodies as subqueries with the full column list (e.g. `JOIN (SELECT <all cols> FROM t WHERE ...) AS t`, `EXISTS (SELECT <all cols> ...)`). R3 computes the columns used above (projection, predicates, join keys, group, order) and narrows each inner Project to exactly those columns, or to `SELECT 1` inside EXISTS. Preconditions (structural): the inner Project is not DISTINCT (pruning under DISTINCT changes cardinality), and the pruned columns aren't referenced above. It also applies to LLM SQL that converted to IR.
- **R4 redundant join elimination.** It drops `Join(A, B, A.fk = B.key)` when (a) no B column is used above (projection, filters, group, order, having) except B.key; (b) the edge is a declared, non-violated FK A.fk → B.key; (c) A.fk is NOT NULL (all columns, if composite); (d) B.key is B's declared PK or UNIQUE; (e) it is an inner join. Extension, still sound: references to `B.key` above are rewritten to `A.fk` (equal on every surviving row), so "show students of department 3"-style queries also qualify. Each A row then matches exactly one B row, so the join neither filters nor duplicates. The trace lists all five checks with evidence (e.g. `NOT NULL: student.dept_id ✓`).
- **`.sql` dumps.** Pre-clean with regexes: drop `/*! ... */` conditional comments, `SET ...;`, `LOCK/UNLOCK TABLES`, `DELIMITER`, and `CREATE DATABASE/USE`. Then split statements with sqlglot, transpile each with `read="mysql", write="sqlite"`, and strip table options (`ENGINE`, `CHARSET`, `AUTO_INCREMENT=`, `COLLATE`) at the AST level. Secondary `KEY idx (...)` definitions become separate `CREATE INDEX`. Inline and `CONSTRAINT ... FOREIGN KEY` definitions are kept so they're introspectable. Statements load into a temp SQLite file in one transaction, with FK enforcement off during the load and `foreign_key_check` afterwards. Statements that fail to transpile are reported as load diagnostics, not crashes. Dialect detection is lightweight (backticks/ENGINE → mysql, otherwise generic); MySQL is the tested path.
- **`nokeys.db`**: two or three tables with no declared PK/FK but `x_id`-style naming, plus a deliberate duplicate row. The tests and demo show: joins still work through `inferred` edges, R1 still fires (needs no constraints), and R2/R4 appear in `skipped` with "edge is inferred" / "no declared PK".
- **Chinook**: downloaded from the `lerocha/chinook-database` GitHub releases into `samples/chinook.db`. If the network fails, it's skipped and noted. It's used only as an **unseen schema** smoke test (PascalCase names like `InvoiceLine` and `CustomerId` exercise identifier splitting and naming inference), not in the graded demo list.
- **datagen.py** (`python datagen.py in.db out.db --rows 100000 [--seed 0]`):
  - Copies the DDL, then fills tables in FK topological order. Cycles are broken by inserting with nullable FK = NULL and patching afterwards; a NOT NULL cycle is reported.
  - Integer single-column PKs are sequential. Composite PKs are unique tuples drawn from parent key spaces, with N capped at the product of parent cardinalities and a warning.
  - FK columns are sampled from parent keys. Nullable FKs are NULL ~5% of the time; NOT NULL columns are never NULL. UNIQUE columns are tracked in sets.
  - Values are driven by the existing data, never by column names: numerics uniform within the observed [min, max] (integer vs real preserved); low-cardinality text resampled from existing distinct values; high-cardinality text from Faker words or sentences plus a uniqueness suffix where needed; dates within the observed range.
  - Fast path: `executemany` in 10k-row batches, `PRAGMA synchronous=OFF`, `journal_mode=OFF`, one transaction per table.
- **Soundness check on scaled data**: `test_soundness_scaled.py` runs datagen on university and retail (small N for test speed), then runs every demo compiler question against the scaled DBs and asserts equivalence passes and every fired rule's preconditions hold. Fresh data gives a second line of evidence beyond the hand-built samples.

**Verification**
- R3/R4 positive tests, plus R4 negatives: nullable FK (the sample schema deliberately has one nullable FK) → skipped; inferred edge (nokeys) → skipped; violated FK fixture → skipped; a B column used above → skipped.
- The dump test loads `retail_dump.sql` and checks the same tables, row counts and declared FKs as `retail.db`, and that a demo question gives identical results on both.
- datagen test: N=500 per table; `foreign_key_check` is empty, NOT NULL and PK uniqueness hold, row counts are right. Plus one manual run at `--rows 100000` on `university.db`, with wall time recorded in PROGRESS.md.
- `python -m nl2sql "..." --db samples/chinook.db` for a few questions (smoke, non-graded).
- Commit `M6`.

**Risks**
- sqlglot MySQL→SQLite transpile gaps (e.g. `ENUM`, `UNSIGNED`, `ON UPDATE CURRENT_TIMESTAMP`). Mitigation: AST-level cleanup for the known clauses, plus a per-statement error report. The bundled dump uses common real-world mysqldump constructs so the path is exercised.
- Naive SQL that looks artificially bad (to give R3 work). Mitigation: the naive form is a faithful literal translation (a filtered input becomes a subquery), and PROGRESS.md states this openly.
- datagen speed for 100k rows × composite keys. Mitigation: batching; sampling tuples without replacement using sets.

---

### M7 — Everything green, demo check, README

**Files**
- `demo_questions.md`, `scripts/demo_check.py`, `README.md`, `PROGRESS.md` (final), `tests/test_no_hardcoded_names.py`, `tests/test_demo_questions.py`

**Key design decisions**
- **`demo_questions.md`** format: `## <db file>` → `### compiler` / `### llm` → `- question  <!-- expect: R2 -->`. That's ≥8 compiler questions per DB (university, retail), plus 2 LLM questions per DB. The R1–R4 annotations guarantee each rule fires at least once per DB where the schema allows. A `nokeys.db` section shows rules being skipped (bonus, not required).
- **`scripts/demo_check.py`**: for each question, runs `compile()` and prints `PASS`/`FAIL` with source, fired rules and the reason. A compiler question PASSes only if `source == "compiler"` (it must not silently fall back to the LLM), it executes, equivalence passes, and the annotated rules fired. An LLM question PASSes if it ran and validated, or if it returned `fallback_unavailable` cleanly. The script exits non-zero if any compiler question fails.
- **Hardcoded-name test**: collects every table and column name from all sample DBs (and Chinook if present). It then scans `nl2sql/**/*.py` string literals and comments (via `tokenize`) and whole-word identifiers. The allowlist is only `id` (the spec's inferred-key convention). Generic column words that are also Python attributes (e.g. `name`) are checked only inside string literals and comments, where hardcoding would actually live. The same check runs as a plain `grep -rwiE` in the README's verification section.
- **README**: setup on Windows and POSIX (venv, `pip install -r requirements.txt`, spaCy model download, `.env` keys), rebuilding samples, CLI examples, `streamlit run app.py`, datagen, running tests and demo_check, the architecture diagram, the rule table with preconditions, and limitations.
- **PROGRESS.md**: every milestone with a done/not-done status and its commit, the choices log (everything marked "decision" in this plan), the resolved Gemini model, and known limitations (controlled grammar coverage, no self-joins, no OR across tables in the compiler path, LLM path depends on quota).

**Verification** (the Definition of Done, run in order)
1. `.venv\Scripts\python.exe -m pytest` → exit 0
2. `.venv\Scripts\python.exe scripts\demo_check.py` → PASS on every line, exit 0
3. `grep -rwiE "<all sample table|column names>" nl2sql/` → no hits (besides the documented `id`)
4. Fresh-clone check: copy the repo to a scratch dir, create a venv, install, run steps 1–2
5. Final commit `M7`.

**Risks**
- Demo questions phrased beyond the grammar. Mitigation: questions are written alongside the grammar tests from M2 on, and any failure is fixed in the grammar, not by rewording to dodge a bug.
- Test-suite runtime (spaCy load + scaled data). Mitigation: a session-scoped spaCy fixture and small scaled sizes in tests.

---

## 3. Sample database design (`scripts/make_samples.py`, seeded, deterministic)

**university.db** — `id`-style keys
- `department(id PK, name TEXT NOT NULL UNIQUE, building, budget REAL)`. About 8 rows with real abbreviations as names (CSE, ECE, MECH, CIVIL, PHY, MATH, ...). A couple of departments have no courses, so semi-join answers are informative.
- `faculty(id PK, name NOT NULL, dept_id INTEGER NOT NULL → department.id, designation, salary REAL)`. About 60 rows.
- `student(id PK, name NOT NULL, dept_id INTEGER NOT NULL → department.id, gpa REAL, year INTEGER, city)`. About 2,000 rows.
- `course(id PK, title NOT NULL, dept_id NOT NULL → department.id, faculty_id INTEGER **NULL** → faculty.id, credits INTEGER)`. About 120 rows; the nullable FK is the R4 negative case.
- `enrollment(student_id NOT NULL → student.id, course_id NOT NULL → course.id, semester, grade, PK(student_id, course_id, semester))`. About 15,000 rows; composite PK and 2-hop join paths.
- Two students share a name, so R2's DISTINCT-vs-PK nuance shows up in the data.

**retail.db** — `<table>_id`-style keys, plural table names (a different naming style on purpose)
- `customers(customer_id PK, name, email UNIQUE, city, country, signup_date)`, with some customers having no orders
- `products(product_id PK, name, category, price REAL NOT NULL, stock INTEGER)`
- `orders(order_id PK, customer_id NOT NULL → customers, order_date, status, total REAL)`
- `order_items(order_id NOT NULL → orders, product_id NOT NULL → products, quantity, unit_price, PK(order_id, product_id))`

**retail_dump.sql** is generated from the same data as a mysqldump-style file: backticks, `ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`, `AUTO_INCREMENT`, `KEY` indexes, `CONSTRAINT ... FOREIGN KEY`, `LOCK TABLES`, `SET` headers, `/*!40101 ... */` comments, multi-row INSERTs.

**nokeys.db**: e.g. `team(id, name)`, `player(id, name, team_id, score)`, `match_result(...)`. No PK/FK/NOT NULL is declared, and the data includes one duplicate row.

---

## 4. Global risks (summary)

| Risk | Impact | Mitigation |
|---|---|---|
| spaCy / model install fails on Windows | Lexer unusable | Python 3.12 wheels; `sm` fallback; recorded in PROGRESS.md |
| Controlled grammar too narrow | Demo questions fall to LLM | Grammar grown test-first; demo_check fails if a compiler question uses the LLM |
| Declared FKs violated by data (SQLite doesn't enforce) | R2/R4 unsound | `foreign_key_check` at load demotes violated FKs; equivalence check on every query |
| R2 DISTINCT semantics | Wrong results | DISTINCT dropped only when a non-null PK is projected |
| sqlglot API churn | Codegen breakage | Exact version pin; sqlglot isolated behind helpers |
| Gemini unavailable (key, quota, network, model names) | Fallback fails | Runtime model resolution, disk cache, `fallback_unavailable` result, never raises |
| `.env` key leaking into git, cache or logs | Secret exposure | `.gitignore` before first commit; key never stored or printed |
| Hardcoded-name leak via docstrings/examples | Fails DoD | Invented names in docs; automated tokenize-based test |
| Windows specifics (file locks, console encoding, paths with spaces) | Flaky runs | Explicit connection closing, UTF-8 stdout, `pathlib` everywhere, venv inside `C:\NL2SQL` |

---

## 5. Deviations / extras relative to SPEC.md (to be logged in PROGRESS.md)

- `scripts/make_samples.py` (sample builder) and `nl2sql/sql_to_ir.py` (LLM SQL → IR helper) are extra files beyond the spec layout.
- R2 is **stricter** than the spec: DISTINCT is kept unless a non-null PK is projected.
- R4 is **slightly broader** than the spec: references to the referenced key are rewritten to the FK column, which is sound under the same preconditions.
- Declared-but-violated FKs are treated like inferred edges.
- The optimizer trace also records **skipped** rules with the failing precondition, not just firings.
- Chinook is a non-graded smoke test.
