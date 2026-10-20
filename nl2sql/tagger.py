"""Map tokens onto schema elements, with a confidence per tag.

Everything the tagger matches against is derived at runtime from the
:class:`~nl2sql.schema.SymbolTable`. No element name appears in this module.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable

from .lexer import Token
from .schema import Column, Database, Table, pluralize_forms, singularize, quote_ident

VECTOR_THRESHOLD = 0.70
MAX_NGRAM = 3

# Grammar words. These steer the parser and must never be read as data.
AGGREGATE_WORDS = {
    "count": "COUNT", "number": "COUNT", "how many": "COUNT", "total number": "COUNT",
    "average": "AVG", "avg": "AVG", "mean": "AVG",
    "sum": "SUM", "total": "SUM",
    "minimum": "MIN", "min": "MIN", "smallest": "MIN", "lowest": "MIN",
    "maximum": "MAX", "max": "MAX", "largest": "MAX", "highest": "MAX",
    "biggest": "MAX", "greatest": "MAX",
}

SUPERLATIVE_DESC = {"highest", "largest", "biggest", "greatest", "maximum", "max", "top", "best", "most"}
SUPERLATIVE_ASC = {"lowest", "smallest", "minimum", "min", "least", "cheapest", "fewest"}

COMMAND_WORDS = {"show", "list", "display", "find", "get", "give", "tell", "fetch",
                 "select", "return", "print", "retrieve"}
GROUP_WORDS = {"per", "each", "every", "by"}
ORDER_WORDS = {"sorted", "ordered", "order", "sort", "arranged", "rank", "ranked"}
EXISTENCE_OPENERS = {"is", "are", "does", "do", "any"}
DISTINCT_WORDS = {"distinct", "unique", "different"}
DESC_WORDS = {"descending", "desc", "decreasing"}
ASC_WORDS = {"ascending", "asc", "increasing"}

STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "with", "and", "or",
    "that", "which", "who", "whom", "whose", "what", "where", "there", "their",
    "is", "are", "was", "were", "be", "been", "being", "has", "have", "had",
    "do", "does", "did", "it", "its", "from", "as", "all", "me", "us", "please",
    "than", "then", "s", "'s", "whats", "value", "values", "records", "rows",
    "many", "much", "much", "also", "only", "not", "no", "they",
}

# Words that steer the grammar; never schema matches.
GRAMMAR_WORDS = (
    set(AGGREGATE_WORDS)
    | COMMAND_WORDS
    | GROUP_WORDS
    | ORDER_WORDS
    | DISTINCT_WORDS
    | DESC_WORDS
    | ASC_WORDS
    | SUPERLATIVE_DESC
    | SUPERLATIVE_ASC
    | STOPWORDS
)

# Grammar words that lose to an exact schema name (a column may be called
# "total" or "count"); structural words like "by" never lose.
OVERRIDABLE_WORDS = set(AGGREGATE_WORDS) | SUPERLATIVE_DESC | SUPERLATIVE_ASC

_SPLIT_RE = re.compile(r"[_\-\s]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def split_identifier(name: str) -> list[str]:
    """``order_items`` / ``OrderItems`` / ``orderItems`` -> ``[order, items]``."""
    parts = [p for p in _SPLIT_RE.split(name) if p]
    return [p.lower() for p in parts]


@dataclass
class Tag:
    """One candidate interpretation of a token span."""

    kind: str               # table | column | value | aggregate | operator | keyword
    start: int              # first token index
    end: int                # exclusive
    confidence: float
    table: str | None = None
    column: str | None = None
    value: Any = None
    aggregate: str | None = None
    operator: str | None = None
    how: str = ""           # how the match was made, shown in the UI
    text: str = ""

    @property
    def length(self) -> int:
        return self.end - self.start

    def label(self) -> str:
        if self.kind == "table":
            return f"table:{self.table}"
        if self.kind == "column":
            return f"column:{self.table}.{self.column}"
        if self.kind == "value":
            return f"value:{self.table}.{self.column}={self.value!r}"
        if self.kind == "aggregate":
            return f"agg:{self.aggregate}"
        if self.kind == "operator":
            return f"op:{self.operator}"
        return self.kind


@dataclass
class TagResult:
    tokens: list[Token]
    tags: list[Tag] = field(default_factory=list)
    unmatched: list[Token] = field(default_factory=list)

    def at(self, index: int) -> list[Tag]:
        return [t for t in self.tags if t.start <= index < t.end]

    def min_schema_confidence(self) -> float:
        schema_tags = [t for t in self.tags if t.kind in ("table", "column", "value")]
        return min((t.confidence for t in schema_tags), default=1.0)


class Lexicon:
    """Runtime name variants for every table and column in a schema."""

    def __init__(self, db: Database):
        self.db = db
        self.symbols = db.symbols
        self.table_variants: dict[str, list[tuple[str, float, str]]] = {}
        self.column_variants: dict[tuple[str, str], list[tuple[str, float, str]]] = {}
        self._build()

    def _build(self) -> None:
        for tbl in self.symbols.tables.values():
            self.table_variants[tbl.name] = self._variants(tbl.name)
            for col in tbl.columns.values():
                self.column_variants[(tbl.name, col.name)] = self._variants(col.name)

    @staticmethod
    def _variants(name: str) -> list[tuple[str, float, str]]:
        """(surface form, confidence, how) pairs for one identifier."""
        out: dict[str, tuple[float, str]] = {}

        def add(form: str, conf: float, how: str) -> None:
            form = form.strip().lower()
            if not form:
                return
            if form not in out or out[form][0] < conf:
                out[form] = (conf, how)

        low = name.lower()
        add(low, 1.0, "exact name")
        parts = split_identifier(name)
        joined = " ".join(parts)
        add(joined, 1.0, "split identifier")
        add("".join(parts), 0.97, "joined identifier")
        sing = singularize(joined)
        add(sing, 0.95, "singular form")
        for form in pluralize_forms(sing):
            add(form, 0.95, "plural form")
        if len(parts) > 1:
            tail = parts[-1]
            add(tail, 0.75, "last word of identifier")
            add(singularize(tail), 0.72, "last word, singular")
            head = " ".join(parts[:-1])
            add(head, 0.7, "leading words of identifier")
        return [(form, conf, how) for form, (conf, how) in out.items()]


class Tagger:
    def __init__(self, db: Database):
        self.db = db
        self.symbols = db.symbols
        self.lexicon = Lexicon(db)
        self._vector_cache: dict[tuple[str, str], float] = {}
        # Surface forms that name a real schema element in this database.
        self._schema_forms: set[str] = set()
        for variants in self.lexicon.table_variants.values():
            self._schema_forms.update(f for f, c, _ in variants if c >= 0.95)
        for variants in self.lexicon.column_variants.values():
            self._schema_forms.update(f for f, c, _ in variants if c >= 0.95)

    def _overridden(self, token: Token) -> bool:
        """True when a grammar word also names something in this schema."""
        return (
            token.lower in OVERRIDABLE_WORDS
            and token.lower in self._schema_forms
        )

    # -- public ---------------------------------------------------------
    def tag(self, tokens: list[Token]) -> TagResult:
        """Tag a token stream, strongest evidence first.

        Exact schema names are claimed before aggregate keywords, so a column
        called ``score_avg`` is not broken apart by the word "avg". A keyword
        that also names something here is emitted twice, leaving the choice
        to the parser.
        """
        result = TagResult(tokens=list(tokens))
        consumed: set[int] = set()

        for tok in tokens:
            if tok.is_operator:
                result.tags.append(
                    Tag("operator", tok.index, tok.index + 1, 1.0,
                        operator=tok.operator, how="comparison phrase", text=tok.text)
                )
                consumed.add(tok.index)

        result.tags += self._sweep(tokens, consumed, self._match_names, MAX_NGRAM,
                                   self._is_grammar_span)
        result.tags += self._tag_aggregates(tokens, consumed)
        result.tags += self._sweep(tokens, consumed, self._match_values, MAX_NGRAM,
                                   self._is_all_grammar)
        result.tags += self._sweep(tokens, consumed, self._match_fuzzy, 1,
                                   self._is_grammar_span)

        tagged = {i for t in result.tags for i in range(t.start, t.end)}
        for tok in tokens:
            if tok.index in tagged or tok.is_num:
                continue
            if tok.lower in GRAMMAR_WORDS or tok.lemma in GRAMMAR_WORDS:
                continue
            if tok.pos in ("NOUN", "PROPN", "ADJ", "X") and len(tok.lower) > 1:
                result.unmatched.append(tok)

        result.tags.sort(key=lambda t: (t.start, -t.confidence))
        return result

    # -- aggregates -----------------------------------------------------
    def _tag_aggregates(self, tokens: list[Token], consumed: set[int]) -> list[Tag]:
        tags: list[Tag] = []
        i = 0
        while i < len(tokens):
            matched = False
            for n in (2, 1):
                if i + n > len(tokens):
                    continue
                window = tokens[i : i + n]
                phrase = " ".join(t.lower for t in window)
                if phrase not in AGGREGATE_WORDS:
                    continue
                # "number" only counts as COUNT when it reads as "number of".
                if phrase == "number" and not self._followed_by(tokens, i + n, "of"):
                    continue
                claimed = any(t.index in consumed for t in window)
                if claimed and not all(self._overridden(t) for t in window):
                    continue
                tags.append(
                    Tag("aggregate", i, i + n, 1.0,
                        aggregate=AGGREGATE_WORDS[phrase],
                        how="aggregate keyword", text=phrase)
                )
                if not claimed:
                    consumed.update(range(i, i + n))
                i += n
                matched = True
                break
            if not matched:
                i += 1
        return tags

    @staticmethod
    def _followed_by(tokens: list[Token], index: int, word: str) -> bool:
        return index < len(tokens) and tokens[index].lower == word

    # -- schema elements -------------------------------------------------
    def _sweep(self, tokens: list[Token], consumed: set[int], matcher,
               max_size: int, skip) -> list[Tag]:
        """Longest-span-first sweep with one matcher, marking spans consumed."""
        tags: list[Tag] = []
        for size in range(max_size, 0, -1):
            i = 0
            while i <= len(tokens) - size:
                window = tokens[i : i + size]
                if any(t.index in consumed or t.is_operator for t in window):
                    i += 1
                    continue
                if skip(window):
                    i += 1
                    continue
                found = matcher(window, i, i + size)
                if found:
                    found = self._dedupe(found)
                    tags.extend(found)
                    consumed.update(range(i, i + size))
                    i += size
                else:
                    i += 1
        return tags

    def _is_all_grammar(self, window: list[Token]) -> bool:
        """For values: only a span made entirely of grammar words is skipped.

        A real value may well contain one ("Block A", "Out of Stock"), so the
        per-token rule used for names is too strict here.
        """
        if any(t.is_quoted for t in window):
            return False
        return all(
            t.lower in GRAMMAR_WORDS or t.lemma in GRAMMAR_WORDS for t in window
        )

    def _is_grammar_span(self, window: list[Token]) -> bool:
        """A span holding a grammar word is never a schema or value match."""
        phrase = " ".join(t.lower for t in window)
        lemma_phrase = " ".join(t.lemma for t in window)
        # A multi-word identifier may legitimately end in a grammar word
        # ("score avg"), so a whole-span name match wins.
        if phrase in self._schema_forms or lemma_phrase in self._schema_forms:
            return False
        for tok in window:
            if tok.is_quoted or self._overridden(tok):
                continue
            # A surface form that names something in this schema and is not
            # itself a grammar word wins over its lemma (a table of orders
            # lemmatises to the grammar word "order").
            if tok.lower not in GRAMMAR_WORDS and tok.lower in self._schema_forms:
                continue
            if tok.lower in GRAMMAR_WORDS or tok.lemma in GRAMMAR_WORDS:
                return True
        return False

    def _match_names(self, window: list[Token], start: int, end: int) -> list[Tag]:
        phrase = " ".join(t.lower for t in window)
        lemma_phrase = " ".join(t.lemma for t in window)
        out: list[Tag] = []
        for tname, variants in self.lexicon.table_variants.items():
            for form, conf, how in variants:
                if form == phrase or form == lemma_phrase:
                    out.append(Tag("table", start, end, conf, table=tname,
                                   how=how, text=phrase))
                    break
        for (tname, cname), variants in self.lexicon.column_variants.items():
            for form, conf, how in variants:
                if form == phrase or form == lemma_phrase:
                    out.append(Tag("column", start, end, conf, table=tname,
                                   column=cname, how=how, text=phrase))
                    break
        return out

    def _match_fuzzy(self, window: list[Token], start: int, end: int) -> list[Tag]:
        tok = window[0]
        if tok.is_num or len(tok.lower) < 3:
            return []
        if tok.pos not in ("NOUN", "PROPN", "ADJ", "VERB", "X", ""):
            return []
        phrase = tok.lower
        hits = self._match_abbreviation(phrase, start, end)
        if hits:
            return hits
        return self._match_vectors(phrase, start, end)

    @staticmethod
    def _dedupe(tags: list[Tag]) -> list[Tag]:
        best = max(t.confidence for t in tags)
        kept = [t for t in tags if t.confidence >= best - 1e-9]
        # A table match outranks an equally-confident column match.
        if any(t.kind == "table" for t in kept) and any(t.kind == "column" for t in kept):
            if max(t.confidence for t in kept if t.kind == "table") >= max(
                t.confidence for t in kept if t.kind == "column"
            ):
                kept = [t for t in kept if t.kind == "table"]
        return kept

    # -- abbreviations ---------------------------------------------------
    def _match_abbreviation(self, phrase: str, start: int, end: int) -> list[Tag]:
        if len(phrase) < 3 or " " in phrase:
            return []
        hits: list[Tag] = []
        for tname, variants in self.lexicon.table_variants.items():
            for form, _, _ in variants:
                if self._is_abbreviation(phrase, form):
                    hits.append(Tag("table", start, end, 0.8, table=tname,
                                    how=f"abbreviation of {form}", text=phrase))
                    break
        for (tname, cname), variants in self.lexicon.column_variants.items():
            for form, _, _ in variants:
                if self._is_abbreviation(phrase, form):
                    hits.append(Tag("column", start, end, 0.8, table=tname,
                                    column=cname, how=f"abbreviation of {form}",
                                    text=phrase))
                    break
        # An ambiguous abbreviation is only kept when the vectors agree.
        if len(hits) > 1:
            scored = []
            for tag in hits:
                target = tag.column or tag.table or ""
                sim = self._similarity(phrase, " ".join(split_identifier(target)))
                if sim >= VECTOR_THRESHOLD:
                    tag.confidence = min(0.85, 0.75 + sim / 10)
                    scored.append(tag)
            return scored
        return hits

    @staticmethod
    def _is_abbreviation(short: str, full: str) -> bool:
        if short == full or len(short) >= len(full) or len(short) < 3:
            return False
        if full.startswith(short):
            return True
        # subsequence match, e.g. "qty" inside "quantity"
        it = iter(full)
        return all(ch in it for ch in short)

    # -- values ----------------------------------------------------------
    def _match_values(self, window: list[Token], start: int, end: int) -> list[Tag]:
        phrase = " ".join(t.text for t in window)
        quoted = any(t.is_quoted for t in window)
        if not quoted:
            raw = phrase.strip('"\'')
        else:
            raw = phrase.strip('"\'')
        if not raw:
            return []
        capitalized = raw[0].isupper() or raw.isupper()
        if len(raw) == 1 and not (quoted or raw.isupper()):
            return []
        if not (quoted or capitalized or len(raw) > 2):
            return []

        hits: list[Tag] = []
        low = raw.lower()
        for col in self.symbols.all_columns():
            if col.type_class not in ("text", "date"):
                continue
            for sample in col.samples:
                if str(sample).lower() == low:
                    hits.append(
                        Tag("value", start, end, 0.9, table=col.table,
                            column=col.name, value=sample,
                            how="matched a sampled value", text=phrase)
                    )
                    break
        if hits:
            return hits
        if quoted or capitalized:
            hits = self._probe_values(raw, start, end, phrase)
        return hits

    def _probe_values(self, raw: str, start: int, end: int, phrase: str) -> list[Tag]:
        """One bounded exact-match lookup per text column, for unsampled values."""
        hits: list[Tag] = []
        try:
            conn = self.db.connect(timeout_seconds=5.0)
        except sqlite3.Error:
            return hits
        try:
            for col in self.symbols.all_columns():
                if col.type_class != "text" or not col.samples:
                    continue
                sql = (
                    f"SELECT {quote_ident(col.name)} FROM {quote_ident(col.table)} "
                    f"WHERE {quote_ident(col.name)} = ? LIMIT 1"
                )
                try:
                    row = conn.execute(sql, (raw,)).fetchone()
                except sqlite3.Error:
                    continue
                if row is not None:
                    hits.append(
                        Tag("value", start, end, 0.85, table=col.table,
                            column=col.name, value=row[0],
                            how="found by lookup", text=phrase)
                    )
        finally:
            conn.close()
        return hits

    # -- vectors ----------------------------------------------------------
    def _match_vectors(self, phrase: str, start: int, end: int) -> list[Tag]:
        from .lexer import has_vectors

        if not has_vectors() or len(phrase) < 3:
            return []
        best: tuple[float, Tag] | None = None
        for tname in self.symbols.tables:
            sim = self._similarity(phrase, " ".join(split_identifier(tname)))
            if sim >= VECTOR_THRESHOLD and (best is None or sim > best[0]):
                best = (sim, Tag("table", start, end, round(sim, 3), table=tname,
                                 how=f"word vectors ({sim:.2f})", text=phrase))
        for col in self.symbols.all_columns():
            sim = self._similarity(phrase, " ".join(split_identifier(col.name)))
            if sim >= VECTOR_THRESHOLD and (best is None or sim > best[0]):
                best = (sim, Tag("column", start, end, round(sim, 3), table=col.table,
                                 column=col.name, how=f"word vectors ({sim:.2f})",
                                 text=phrase))
        return [best[1]] if best else []

    def _similarity(self, a: str, b: str) -> float:
        from .lexer import has_vectors, load_nlp

        if not has_vectors() or not a or not b:
            return 0.0
        key = (a, b)
        if key in self._vector_cache:
            return self._vector_cache[key]
        nlp = load_nlp()
        da, db_ = nlp(a), nlp(b)
        sim = 0.0
        if da.vector_norm and db_.vector_norm:
            sim = float(da.similarity(db_))
        self._vector_cache[key] = sim
        return sim
