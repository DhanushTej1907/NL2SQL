# NL2SQL Compiler

A schema-agnostic natural-language-to-SQL compiler with an **intent-aware,
sound-by-construction optimizer**. Point it at any SQLite file or `.sql`
dump, ask a question in English, and it shows you the tokens, the tags, the
AST, the relational IR, the naive SQL, the optimized SQL, every rewrite it
made and why it was allowed to make it, and proof that the two queries
return the same rows.

No table or column name is hardcoded anywhere in `nl2sql/`. Everything the
compiler knows about a database it learns by introspecting it at load time.

```
question ─► lexer ─► tagger ─► parser ─► semantic ─► IR ─► optimizer ─► codegen ─► execute
              │         │         │          │         │         │          │         │
           tokens     tags       AST    diagnostics  naive   optimized   naive/opt  rows,
                                        join paths     IR     IR+trace     SQL      plans,
                 ▲                                                               equivalence
                 └── low confidence, or a parse or semantic error ──► LLM fallback ──┘
                                                       (Gemini → sqlglot → the same back end)
```

## Setup

Python 3.10 or newer; 3.12 is what this was built and tested on.

**Windows (PowerShell)**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m spacy download en_core_web_md
.\.venv\Scripts\python.exe scripts\make_samples.py
```

**macOS / Linux**

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m spacy download en_core_web_md
.venv/bin/python scripts/make_samples.py
```

`en_core_web_md` carries word vectors, which the tagger uses for fuzzy
matching. If it will not download, `en_core_web_sm` works too and vector
matching simply switches itself off.

For the language-model fallback, put a key in `.env` at the project root:

```
GEMINI_API_KEY = your-key-here
# GEMINI_MODEL is optional; without it the newest stable Flash model is
# resolved at runtime and cached in .cache/llm/model.json
```

Everything except the fallback works without a key. `.env` and `.cache/`
are git-ignored, and the key is never logged, cached or returned in a
result.

## Running it

**The demo app**

```bash
streamlit run app.py
```

Pick a bundled sample or upload your own `.sqlite`, `.db` or `.sql`, then
ask a question. The tabs show results, tokens and tags with confidences,
the AST, the naive and optimized IR, the two SQL queries side by side, the
rewrite trace with a checkmark per precondition, `EXPLAIN QUERY PLAN`
before and after, timings and diagnostics. Two badges sit above the tabs:
whether the **compiler** or the **LLM fallback** produced the SQL, and
whether the equivalence check passed.

**The command line**

```bash
python -m nl2sql "which departments have students with gpa over 9.9" \
    --db samples/university.db --show trace,ir,plan

python -m nl2sql "how many orders per customer" --db samples/retail.db
python -m nl2sql "show products in Electronics" --db samples/retail_dump.sql
python -m nl2sql "top 5 students by gpa" --db samples/university.db --dialect postgres
python -m nl2sql "is there any player with score above 95" --db samples/nokeys.db --show trace
```

Options: `--show tokens,tags,ast,ir,optimized,trace,plan,schema,diagnostics`,
`--dialect <sqlglot dialect>`, `--json`, `--no-llm`, `--rows N`.

**Scaling a database up**

```bash
python datagen.py samples/university.db big.db --rows 100000 --seed 0
```

Fills every table to N rows respecting primary keys, foreign keys, NOT NULL
and UNIQUE. Values are drawn from the shape of the data already there
(observed ranges for numbers, resampled pools for low-cardinality text,
Faker for the rest), never from column names.

## The rules

Every rule fires only when all of its preconditions hold, and only ever on
**declared** constraints. An inferred join edge never satisfies a
precondition, and a declared foreign key whose data breaks it is demoted to
`declared_violated` and treated as inferred. Each firing appends the rule
name, the whole query before and after, and every precondition with its
evidence; a rule that matched but could not fire is recorded too, with the
check that failed.

| Rule | Rewrite | Preconditions |
|---|---|---|
| **R1** existence | `SELECT COUNT(*) > 0 …` → `SELECT EXISTS (SELECT 1 …)` | the comparison is a genuine non-emptiness test; no GROUP BY or HAVING under the aggregate. Needs no schema constraints, so it fires even on a key-less database. |
| **R2** semi-join | `DISTINCT outer FROM outer JOIN inner` → `outer WHERE EXISTS (…)` | only outer-table columns are projected; no inner column is used above; the edge is a declared, unviolated FK; the outer table has a declared PK; the join is inner. `DISTINCT` is dropped only when the outer table's full, non-null PK is projected — otherwise it stays. |
| **R3** column pruning | narrow an inner projection to the columns read above; an EXISTS body becomes `SELECT 1` | the projection is not DISTINCT, it sits in a subquery position (never the user's own projection), and something would actually be dropped. Structural, so it needs no declared constraints. |
| **R4** redundant join | drop `A JOIN B ON A.fk = B.key` and rewrite `B.key` above to `A.fk` | no other B column is read above; the edge is a declared, unviolated FK; the FK columns are NOT NULL; the referenced columns are a unique, non-null key; the join is inner; and **B carries no filter of its own** — a filtered inner side can remove rows, so such a join is not redundant. |

Top-k is a compile-time choice, not a rewrite: "top 5" lowers straight to
`ORDER BY … LIMIT 5`.

The naive SQL is a deliberately literal translation of the wording — a
joined table becomes a derived table projecting all its columns, and an
existence question becomes a count — so the optimizer has honest work to
do. The optimized SQL is what actually runs.

## Safety and soundness

- Every query runs on a **read-only** connection with a wall-clock guard,
  after a sqlglot check that it is a single `SELECT`. This applies to SQL
  the language model wrote as much as to the compiler's own.
- Ordered queries get the base table's primary key appended as a
  tiebreaker in **both** the naive and the optimized SQL, so `LIMIT` and
  the ordered comparison are deterministic.
- After every compile, both queries run and their results are compared: as
  a multiset normally, as an ordered list when the statement is ordered.
  The result is reported as a badge, pass or fail.
- SQLite does not enforce foreign keys by default, so `PRAGMA
  foreign_key_check` runs at load time and any declared key the data
  breaks loses its privileges.
- A non-`INTEGER PRIMARY KEY` column can hold NULL in SQLite, so every
  rule that needs key uniqueness also requires the key to be NOT NULL or a
  rowid alias.

## The LLM fallback

The fallback triggers on a parse error, a semantic error, tagger confidence
below 0.6, or a content word that matched nothing in the schema. It sends
the compact schema and the question, asks for exactly one SQLite `SELECT`,
and caches the answer on disk under `(schema hash, question, model)`.

What comes back is parsed by sqlglot and converted into the IR where the
subset allows, and then goes through the same semantic checks, the same
optimizer and the same equivalence check as the compiler's own output. SQL
using constructs the IR does not model — window functions, CTEs, outer
joins, correlated subqueries — is kept exactly as written, marked
`unoptimized`, and validated by executing it.

With no key, no network, a quota error or an empty response, the fallback
returns a clear `fallback_unavailable` result. It never raises.
`--no-llm` or `NL2SQL_DISABLE_LLM=1` forces that path.

## Sample databases

| File | What it shows |
|---|---|
| `samples/university.db` | `id`-style keys, a composite primary key, a deliberately nullable foreign key, two students sharing a name, and two departments with no courses |
| `samples/retail.db` | plural table names and `<table>_id` keys — a different naming style, same code |
| `samples/retail_dump.sql` | the same retail data as a mysqldump: backticks, `ENGINE`, `AUTO_INCREMENT`, `KEY` indexes, `LOCK TABLES`, `/*! … */` comments |
| `samples/nokeys.db` | no declared keys at all, plus a duplicate row — R1 still fires, R2 and R4 are safely skipped |

Rebuild them any time with `python scripts/make_samples.py`.

## Tests and the demo check

```bash
python -m pytest
python scripts/demo_check.py
```

`demo_check.py` runs every question in `demo_questions.md` and prints
`PASS` or `FAIL` for each. A compiler question passes only if it compiled
*without* the fallback, executed, passed the equivalence check, and fired
every rule annotated for it. An LLM question passes if the fallback
answered and validated it, or if it was unavailable and said so cleanly.
The script exits non-zero if anything failed.

To check by hand that no schema name is hardcoded:

```bash
grep -rwiE "student|department|enrollment|faculty|course|gpa|dept_id|customers|products|orders|order_items|team|player" --include="*.py" nl2sql/
```

That should print nothing. `tests/test_no_hardcoded_names.py` does the same
job properly, scanning identifiers, string literals and comments with
`tokenize` against the names of every bundled sample.

## Layout

```
nl2sql/
  schema.py        load a SQLite file or .sql dump; introspect -> SymbolTable
  lexer.py         spaCy tokens, lemmas, numbers, comparison phrases
  tagger.py        tokens -> table / column / value / operator / aggregate tags
  parser.py        controlled grammar -> AST
  semantic.py      reference resolution, FK join paths, type checks, diagnostics
  ir.py            relational algebra, intent tags, and AST -> IR lowering
  optimizer.py     R1-R4 with preconditions and a rewrite trace
  codegen.py       IR -> sqlglot -> SQL in the target dialect
  sql_to_ir.py     the model's SQL -> IR, for the supported subset
  llm_fallback.py  Gemini fallback with an on-disk cache
  equivalence.py   run both queries, compare the result multisets
  pipeline.py      compile(question, db) -> CompileResult
  __main__.py      the CLI
app.py             the Streamlit demo
datagen.py         schema-driven data generator
scripts/           make_samples.py, demo_check.py
samples/           the bundled databases
tests/             the test suite
```

## Known limitations

See `PROGRESS.md` for the full list, the choices made along the way, and
the status of each milestone.
