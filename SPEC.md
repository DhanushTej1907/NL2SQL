# NL2SQL Compiler — Review 2 build spec

Build a **schema-agnostic natural-language-to-SQL compiler** in Python with an **intent-aware, sound-by-construction optimizer**. It must work on any uploaded SQLite file or `.sql` dump. No table or column name may be hardcoded anywhere in `nl2sql/`.

Work autonomously. Do not stop to ask questions: make a reasonable choice, note it in `PROGRESS.md`, and keep going. Commit to git after every milestone. Each milestone must leave the project runnable.

## Stack

- Python 3.10+, in a virtualenv, with `requirements.txt`
- spaCy with `en_core_web_md` (fall back to `en_core_web_sm` if md won't download; word-vector matching then degrades gracefully)
- sqlglot (SQL parsing, dialect output, `.sql` dump transpiling)
- SQLAlchemy (introspection)
- Streamlit (demo UI)
- `google-genai` for the Gemini LLM fallback; key from `GEMINI_API_KEY`, model from `GEMINI_MODEL` (read both from `.env` via python-dotenv). If `GEMINI_MODEL` is unset, list available models with the client and pick a current Flash model. Never hardcode the key.
- pytest

## Layout

```
nl2sql/
  schema.py        load SQLite file or .sql dump; introspect -> SymbolTable
  lexer.py         spaCy tokens, lemmas, POS, numbers, comparison phrases
  tagger.py        tokens -> table / column / value / operator / aggregate tags, with confidence
  parser.py        controlled grammar -> AST
  semantic.py      type checks, FK join paths, diagnostics
  ir.py            relational algebra nodes + intent tags
  optimizer.py     rules with preconditions + rewrite trace
  codegen.py       IR -> SQL via sqlglot in the target dialect
  llm_fallback.py  Gemini fallback, SQL -> IR via sqlglot, on-disk cache
  equivalence.py   run original vs optimized SQL, compare result multisets
  pipeline.py      compile(question, db) -> CompileResult
  __main__.py      CLI: python -m nl2sql "question" --db path
app.py             Streamlit demo
datagen.py         schema-driven data generator
samples/           sample databases (see below)
scripts/demo_check.py
demo_questions.md
tests/
README.md
PROGRESS.md
```

## Module requirements

**schema.py**
- Accept `.sqlite` / `.db` files directly.
- Accept `.sql` dumps: transpile to SQLite with sqlglot (strip engine clauses etc.), load into a temp SQLite file.
- Introspect tables, columns, types, primary keys, foreign keys, NOT NULL.
- Sample up to 50 distinct values per text column for value lookup.
- Build a SymbolTable. Compute a schema hash (used by the LLM cache).

**lexer.py** — spaCy tokens with lemma, POS, numbers; normalize comparison phrases ("more than", "greater than", "above", "at least", "below", "fewer than", "equal to").

**tagger.py** — map words to schema elements without any hardcoded names:
- split identifiers (snake_case, camelCase, abbreviations like `dept` -> department where vectors allow)
- lemma and plural matching
- word-vector similarity above a threshold
- value lookup against sampled values (e.g. "CSE" found in some name column)
- each tag carries a confidence score

**parser.py** — controlled grammar to an AST. Support:
- show / list / which X; selected columns
- filters with = > < >= <= and text equality
- count, avg, sum, min, max; "how many"
- group by ("per", "for each", "by")
- order by ("highest", "lowest", "sorted by"), top-k ("top 5")
- existence ("is there any", "are there any") -> intent tag `existence`
- "which X have ..." -> intent tag `semi_join`
- joins across one or two foreign keys

**semantic.py**
- type checks (numeric comparator on a text column -> error)
- join paths: BFS over declared FKs; if none declared, infer from naming (`x_id` -> `x.id`) and mark the edge `inferred`
- diagnostics with "did you mean" suggestions (difflib)

**ir.py** — nodes: Scan, Select, Project, Join, SemiJoin, AntiJoin, Aggregate, Sort, Limit, Exists. Intent tags: existence, semi_join, top_k, distinct. Pretty-printer for the UI.

**optimizer.py** — each rule fires only when its precondition holds on **declared** constraints. Inferred joins never satisfy a precondition. Every firing appends a trace entry: rule name, before SQL, after SQL, preconditions checked.
- R1 existence: COUNT(*) > 0 -> EXISTS. Always equivalent.
- R2 semi-join: JOIN + DISTINCT -> WHERE EXISTS. Requires: outer table has a declared PK and only outer-table columns are projected.
- R3 column pruning: drop columns unused above inside joins/subqueries.
- R4 redundant join elimination: drop an FK join whose table's columns are unused. Requires: FK column NOT NULL and references a PK.
- Top-k is a compile-time choice ("top 5" -> ORDER BY ... LIMIT 5), not a rewrite.

**codegen.py** — IR -> sqlglot expression -> SQL string in the target dialect (default sqlite). Also generate the naive (unoptimized) SQL for comparison.

**llm_fallback.py**
- Trigger when parsing or semantic analysis fails, or tagger confidence is low.
- Send the schema (tables, columns, types, keys) and question; ask for one SQL query only.
- Parse the SQL with sqlglot into the IR for the supported subset, then run semantic checks and the optimizer on it. If it uses unsupported constructs, keep it as-is, mark it `unoptimized`, and still validate by executing it.
- Cache responses on disk keyed by (schema hash, question).
- If there is no key or no network, degrade gracefully: return a clear "fallback unavailable" result, never crash.

**equivalence.py** — execute original and optimized SQL, compare result multisets (order-insensitive unless ORDER BY), return pass/fail.

**pipeline.py** — `compile(question, db)` returns: tokens, tags, AST, IR, optimized IR, naive SQL, optimized SQL, trace, source (`compiler` or `llm`), result rows, timings, `EXPLAIN QUERY PLAN` before and after, equivalence result, diagnostics.

**app.py (Streamlit)**
- Upload `.sqlite`, `.db` or `.sql`; also a dropdown of the bundled samples
- Show the introspected schema
- Question box
- Tabs: Tokens & tags, AST, IR, Optimized IR, SQL (naive vs optimized side by side), Rewrite trace, Results, Plan before/after, Equivalence badge, timings
- A visible badge saying whether the compiler or the LLM fallback produced the SQL

**datagen.py** — given any SQLite DB, scale every table to N rows respecting PKs, FKs and NOT NULL (Faker or random). CLI: `python datagen.py in.db out.db --rows 100000`.

## Sample databases (create in `samples/`)

1. `university.db` — student, department, faculty, course, enrollment, with declared PKs and FKs and realistic seed data.
2. `retail.db` — customers, orders, products, order_items, with declared PKs and FKs.
3. `retail_dump.sql` — a MySQL-style dump of the retail schema and data, to test the dump path.
4. `nokeys.db` — a small schema with **no** declared keys, to show rules safely not firing.
5. If network allows, also download the Chinook SQLite database.

## Demo questions

`demo_questions.md`: at least 8 questions per sample DB (university, retail) that the compiler handles without the LLM, covering every rule R1–R4, plus 2 questions per DB that should trigger the LLM fallback.

`scripts/demo_check.py`: runs every question in `demo_questions.md`, prints `PASS`/`FAIL` per question (PASS = compiles, executes, and the equivalence check passes), and exits non-zero if any compiler question fails. LLM questions count as PASS if the fallback is unavailable but handled gracefully.

## Build order (vertical slice first)

- **M1** schema + lexer + tagger + parser for simple SELECT/WHERE + codegen + CLI, working on `university.db`
- **M2** aggregates, group/order/top-k, FK joins, semantic diagnostics
- **M3** IR + optimizer R1 and R2 with trace + equivalence check
- **M4** Streamlit app
- **M5** LLM fallback + cache
- **M6** R3, R4, `.sql` dump support, `nokeys.db`, datagen
- **M7** all tests green, `demo_check.py` all PASS, README with run instructions

## Definition of done

- `pytest` exits 0
- `python scripts/demo_check.py` prints PASS for every question
- `grep` finds no sample table or column names hardcoded in `nl2sql/`
- `README.md` explains setup and how to run the CLI and the app
- `PROGRESS.md` lists each milestone as done or not done, choices made, and known limitations
