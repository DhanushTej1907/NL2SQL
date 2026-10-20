"""A controlled-grammar parser over the tag stream.

Grammar (informally):

    Query      := [Command] [Quantifier] Target [Projection] Filter* [Group]
                  [Order] [Limit]
    Projection := ColumnRef ("," | "and" ColumnRef)*
    Filter     := ColumnRef Operator Value | Value | ColumnRef Value
    Group      := ("per" | "for each" | "by") (Table | ColumnRef)
    Order      := ("sorted by" | "ordered by") ColumnRef [asc|desc]
                | Superlative ColumnRef
    Limit      := "top" Number | "first" Number | "bottom" Number

Only English function words appear here. Every table, column and value comes
from the tag stream, which is derived from the live schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .lexer import Token
from .tagger import (
    ASC_WORDS,
    COMMAND_WORDS,
    DESC_WORDS,
    DISTINCT_WORDS,
    GROUP_WORDS,
    ORDER_WORDS,
    SUPERLATIVE_ASC,
    SUPERLATIVE_DESC,
    Tag,
    TagResult,
)

VALUE_QUESTION_OPENERS = {"what", "whats", "how"}
ENTITY_OPENERS = {"which", "who", "whom", "whose", "name"}
TOP_WORDS = {"top", "first"}
BOTTOM_WORDS = {"bottom", "last"}
CONJUNCTIONS = {"and", ",", "or"}


class ParseError(Exception):
    """Raised when the question falls outside the controlled grammar."""

    def __init__(self, message: str, span: tuple[int, int] | None = None):
        super().__init__(message)
        self.message = message
        self.span = span


@dataclass
class ColumnRef:
    """A column mention; the table may still be ambiguous after parsing."""

    candidates: list[Tag] = field(default_factory=list)
    table: str | None = None
    column: str | None = None
    text: str = ""

    @property
    def confidence(self) -> float:
        return max((c.confidence for c in self.candidates), default=1.0)

    def resolved(self) -> bool:
        return self.table is not None and self.column is not None

    def __str__(self) -> str:
        if self.resolved():
            return f"{self.table}.{self.column}"
        names = sorted({f"{c.table}.{c.column}" for c in self.candidates})
        return " | ".join(names) or self.text


@dataclass
class AggregateSpec:
    func: str                       # COUNT | AVG | SUM | MIN | MAX
    column: ColumnRef | None = None  # None means COUNT(*)
    text: str = ""

    def __str__(self) -> str:
        return f"{self.func}({self.column if self.column else '*'})"


@dataclass
class Condition:
    column: ColumnRef
    operator: str
    value: Any
    text: str = ""

    def __str__(self) -> str:
        return f"{self.column} {self.operator} {self.value!r}"


@dataclass
class OrderSpec:
    column: ColumnRef | None = None
    aggregate: AggregateSpec | None = None
    direction: str = "ASC"

    def __str__(self) -> str:
        key = self.aggregate if self.aggregate else self.column
        return f"{key} {self.direction}"


@dataclass
class Query:
    target: str | None = None
    projections: list[ColumnRef] = field(default_factory=list)
    aggregates: list[AggregateSpec] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    group_by: list[ColumnRef] = field(default_factory=list)
    group_tables: list[str] = field(default_factory=list)
    order_by: list[OrderSpec] = field(default_factory=list)
    limit: int | None = None
    distinct: bool = False
    intents: set[str] = field(default_factory=set)
    tables: list[str] = field(default_factory=list)
    question: str = ""

    def referenced_tables(self) -> list[str]:
        out: list[str] = []
        for name in self.tables:
            if name and name not in out:
                out.append(name)
        return out

    def pretty(self) -> str:
        lines = [f"Query(target={self.target}, intents={sorted(self.intents)})"]
        if self.projections:
            lines.append("  project: " + ", ".join(str(p) for p in self.projections))
        for agg in self.aggregates:
            lines.append(f"  aggregate: {agg}")
        for cond in self.conditions:
            lines.append(f"  filter: {cond}")
        if self.group_by or self.group_tables:
            keys = [str(g) for g in self.group_by] + list(self.group_tables)
            lines.append("  group by: " + ", ".join(keys))
        for order in self.order_by:
            lines.append(f"  order by: {order}")
        if self.limit is not None:
            lines.append(f"  limit: {self.limit}")
        if self.distinct:
            lines.append("  distinct: yes")
        return "\n".join(lines)


@dataclass
class Element:
    """One position in the parse stream: a tagged span or a bare word."""

    start: int
    end: int
    tokens: list[Token]
    tags: list[Tag] = field(default_factory=list)

    @property
    def token(self) -> Token:
        return self.tokens[0]

    @property
    def lower(self) -> str:
        return " ".join(t.lower for t in self.tokens)

    def kinds(self) -> set[str]:
        return {t.kind for t in self.tags}

    def of_kind(self, kind: str) -> list[Tag]:
        return [t for t in self.tags if t.kind == kind]

    @property
    def is_number(self) -> bool:
        return len(self.tokens) == 1 and self.tokens[0].is_num

    @property
    def is_operator(self) -> bool:
        return bool(self.of_kind("operator"))


def build_elements(result: TagResult) -> list[Element]:
    """Collapse tokens and their tags into one element per covered span."""
    tokens = result.tokens
    by_start: dict[int, list[Tag]] = {}
    for tag in result.tags:
        by_start.setdefault(tag.start, []).append(tag)

    elements: list[Element] = []
    i = 0
    while i < len(tokens):
        tags = by_start.get(i, [])
        if tags:
            end = max(t.end for t in tags)
            span_tags = [t for t in tags if t.end == end]
            elements.append(Element(i, end, tokens[i:end], span_tags))
            i = end
        else:
            elements.append(Element(i, i + 1, [tokens[i]], []))
            i += 1
    return elements


class Parser:
    def __init__(self, result: TagResult, question: str = ""):
        self.result = result
        self.question = question
        self.elements = build_elements(result)
        self.consumed: set[int] = set()

    # -- helpers ---------------------------------------------------------
    def _free(self, index: int) -> bool:
        return index not in self.consumed

    def _take(self, index: int) -> None:
        self.consumed.add(index)

    def _next_free(self, index: int) -> int | None:
        for j in range(index + 1, len(self.elements)):
            if self._free(j):
                return j
        return None

    # -- entry point -------------------------------------------------------
    def parse(self) -> Query:
        query = Query(question=self.question)
        self._parse_intent(query)
        self._parse_distinct(query)
        self._parse_limit(query)
        self._parse_conditions(query)
        self._parse_order(query)
        self._parse_group(query)
        self._parse_aggregates(query)
        self._parse_target_and_projection(query)
        self._finalise(query)
        return query

    # -- intent ------------------------------------------------------------
    def _parse_intent(self, query: Query) -> None:
        words = [t.lower for t in self.result.tokens]
        if not words:
            raise ParseError("empty question")
        head = words[0]
        if head in ("is", "are", "does", "do", "did", "was", "were"):
            if "any" in words[:4] or "there" in words[:4]:
                query.intents.add("existence")
        if head in ENTITY_OPENERS:
            query.intents.add("entity_question")
        if head in VALUE_QUESTION_OPENERS:
            query.intents.add("value_question")
        for i, el in enumerate(self.elements):
            if el.tags:
                continue
            if el.lower in ("any", "there") and "existence" in query.intents:
                self._take(i)
            elif el.lower in COMMAND_WORDS and i <= 2:
                self._take(i)

    def _parse_distinct(self, query: Query) -> None:
        for i, el in enumerate(self.elements):
            if not el.tags and el.lower in DISTINCT_WORDS:
                query.distinct = True
                self._take(i)

    # -- limit --------------------------------------------------------------
    def _parse_limit(self, query: Query) -> None:
        for i, el in enumerate(self.elements):
            if el.tags or not self._free(i):
                continue
            low = el.lower
            if low not in TOP_WORDS and low not in BOTTOM_WORDS:
                continue
            direction = "DESC" if low in TOP_WORDS else "ASC"
            nxt = self._next_free(i)
            k = 1
            if nxt is not None and self.elements[nxt].is_number:
                k = int(self.elements[nxt].tokens[0].num_value or 1)
                self._take(nxt)
            query.limit = k
            query.intents.add("top_k")
            query.order_by.append(OrderSpec(direction=direction))
            self._take(i)

    # -- conditions ----------------------------------------------------------
    def _parse_conditions(self, query: Query) -> None:
        n = len(self.elements)
        for i in range(n):
            if not self._free(i):
                continue
            el = self.elements[i]
            cols = el.of_kind("column")
            if not cols:
                continue
            j = self._next_free(i)
            if j is None:
                continue
            nxt = self.elements[j]
            operator = None
            if nxt.is_operator:
                operator = nxt.of_kind("operator")[0].operator
                k = self._next_free(j)
                if k is None:
                    raise ParseError(
                        f"comparison after {nxt.lower!r} has no value", nxt.token.span
                    )
                value_el = self.elements[k]
            else:
                value_el = nxt
                k = j
            value = self._literal(value_el, cols)
            if value is None:
                continue
            if operator is None:
                operator = "="
            query.conditions.append(
                Condition(ColumnRef(cols, text=el.lower), operator, value,
                          text=f"{el.lower} {operator} {value}")
            )
            self._take(i)
            if nxt.is_operator:
                self._take(j)
            self._take(k)

        # A bare value tag carries its own column ("students in CSE").
        for i in range(n):
            if not self._free(i):
                continue
            values = self.elements[i].of_kind("value")
            if not values:
                continue
            best = max(values, key=lambda t: t.confidence)
            same = [v for v in values if v.confidence >= best.confidence - 1e-9]
            refs = [
                Tag("column", v.start, v.end, v.confidence, table=v.table,
                    column=v.column, how=v.how, text=v.text)
                for v in same
            ]
            query.conditions.append(
                Condition(ColumnRef(refs, text=best.text), "=", best.value,
                          text=f"{best.table}.{best.column} = {best.value!r}")
            )
            self._take(i)

    def _literal(self, el: Element, cols: list[Tag]) -> Any:
        """Read a literal from an element, if it can be one."""
        if el.is_number:
            return el.tokens[0].value()
        values = el.of_kind("value")
        if values:
            # Prefer a sampled value belonging to one of the named columns.
            names = {(c.table, c.column) for c in cols}
            for tag in values:
                if (tag.table, tag.column) in names:
                    return tag.value
            return max(values, key=lambda t: t.confidence).value
        if el.tags:
            return None
        tok = el.token
        if tok.is_quoted:
            return tok.text.strip("\"'")
        if tok.text[:1].isupper() or tok.text.isupper():
            return tok.text
        return None

    # -- grouping -------------------------------------------------------------
    def _parse_group(self, query: Query) -> None:
        for i, el in enumerate(self.elements):
            if el.tags or not self._free(i):
                continue
            low = el.lower
            if low not in GROUP_WORDS:
                continue
            if low == "by" and self._is_order_by(i):
                continue
            j = self._next_free(i)
            if j is None:
                continue
            target = self.elements[j]
            if target.lower in ("each", "every"):
                self._take(j)
                j2 = self._next_free(j)
                if j2 is None:
                    continue
                target = self.elements[j2]
                j = j2
            cols = target.of_kind("column")
            tables = target.of_kind("table")
            if cols:
                query.group_by.append(ColumnRef(cols, text=target.lower))
            elif tables:
                best = max(tables, key=lambda t: t.confidence)
                query.group_tables.append(best.table or "")
                query.tables.append(best.table or "")
            else:
                continue
            self._take(i)
            self._take(j)

    def _is_order_by(self, index: int) -> bool:
        """``by`` belongs to an ORDER clause when an order word precedes it."""
        for j in range(index - 1, -1, -1):
            el = self.elements[j]
            if el.tags:
                return False
            if el.lower in ORDER_WORDS:
                return True
            if el.lower in ("the", "in"):
                continue
            return False
        return False

    # -- ordering ---------------------------------------------------------------
    def _parse_order(self, query: Query) -> None:
        pending = [o for o in query.order_by if o.column is None and o.aggregate is None]

        # "top 5 X by Y": the bare "by" belongs to the pending ordering.
        for i, el in enumerate(self.elements):
            if not pending or el.tags or not self._free(i) or el.lower != "by":
                continue
            j = self._next_free(i)
            if j is None:
                continue
            cols = self.elements[j].of_kind("column")
            if not cols:
                continue
            spec = pending.pop(0)
            spec.column = ColumnRef(cols, text=self.elements[j].lower)
            direction = self._direction_after(j)
            if direction is not None:
                spec.direction = direction
            self._take(i)
            self._take(j)

        for i, el in enumerate(self.elements):
            if el.tags or not self._free(i):
                continue
            low = el.lower
            if low not in ORDER_WORDS:
                continue
            j = self._next_free(i)
            if j is not None and self.elements[j].lower == "by" and not self.elements[j].tags:
                self._take(j)
                j = self._next_free(j)
            if j is None:
                continue
            cols = self.elements[j].of_kind("column")
            if not cols:
                continue
            direction = self._direction_after(j, default="ASC")
            spec = pending.pop(0) if pending else OrderSpec()
            if spec not in query.order_by:
                query.order_by.append(spec)
            spec.column = ColumnRef(cols, text=self.elements[j].lower)
            if direction is not None:
                spec.direction = direction
            self._take(i)
            self._take(j)

        # Superlatives: "the highest gpa", "cheapest product".
        for i, el in enumerate(self.elements):
            if not self._free(i):
                continue
            low = el.lower
            if low not in SUPERLATIVE_DESC and low not in SUPERLATIVE_ASC:
                continue
            if el.of_kind("column") or el.of_kind("table"):
                continue
            direction = "DESC" if low in SUPERLATIVE_DESC else "ASC"
            j = self._next_free(i)
            col_el = self.elements[j] if j is not None else None
            cols = col_el.of_kind("column") if col_el else []
            if not cols:
                continue
            spec = pending.pop(0) if pending else OrderSpec()
            if spec not in query.order_by:
                query.order_by.append(spec)
            spec.column = ColumnRef(cols, text=col_el.lower)
            spec.direction = direction
            if query.limit is None:
                query.limit = 1
                query.intents.add("top_k")
            self._take(i)
            self._take(j)

        for spec in list(query.order_by):
            if spec.column is None and spec.aggregate is None:
                query.order_by.remove(spec)

    def _direction_after(self, index: int, default: str | None = None) -> str | None:
        j = self._next_free(index)
        if j is None:
            return default
        low = self.elements[j].lower
        if low in DESC_WORDS:
            self._take(j)
            return "DESC"
        if low in ASC_WORDS:
            self._take(j)
            return "ASC"
        return default

    # -- aggregates ----------------------------------------------------------------
    def _parse_aggregates(self, query: Query) -> None:
        for i, el in enumerate(self.elements):
            if not self._free(i):
                continue
            aggs = el.of_kind("aggregate")
            if not aggs:
                continue
            # A word that also names a column here is only an aggregate when a
            # column or table follows it.
            func = aggs[0].aggregate or "COUNT"
            j = self._next_free(i)
            target_el = self.elements[j] if j is not None else None
            if target_el is not None and target_el.lower == "of":
                self._take(j)
                j = self._next_free(j)
                target_el = self.elements[j] if j is not None else None

            if el.of_kind("column") and (
                target_el is None or not (target_el.of_kind("column") or target_el.of_kind("table"))
            ):
                continue  # read it as the column it names, not as an aggregate

            column_ref = None
            if target_el is not None and target_el.of_kind("column"):
                column_ref = ColumnRef(target_el.of_kind("column"), text=target_el.lower)
                self._take(j)
            elif target_el is not None and target_el.of_kind("table"):
                tbl = max(target_el.of_kind("table"), key=lambda t: t.confidence)
                query.tables.append(tbl.table or "")
                if query.target is None:
                    query.target = tbl.table
                self._take(j)
                if func != "COUNT":
                    raise ParseError(
                        f"{func} needs a column, not a table", el.token.span
                    )
            if func != "COUNT" and column_ref is None:
                raise ParseError(f"{func} has no column to work on", el.token.span)
            query.aggregates.append(AggregateSpec(func, column_ref, text=el.lower))
            self._take(i)

    # -- target and projection ----------------------------------------------------
    def _parse_target_and_projection(self, query: Query) -> None:
        table_hits: list[Tag] = []
        column_hits: list[list[Tag]] = []
        for i, el in enumerate(self.elements):
            if not self._free(i):
                continue
            tables = el.of_kind("table")
            cols = el.of_kind("column")
            if tables:
                best = max(tables, key=lambda t: t.confidence)
                table_hits.append(best)
                self._take(i)
            elif cols:
                column_hits.append(cols)
                self._take(i)

        for tag in table_hits:
            query.tables.append(tag.table or "")
        if query.target is None and table_hits:
            query.target = table_hits[0].table

        for cols in column_hits:
            query.projections.append(ColumnRef(cols, text=cols[0].text))

        if query.target is None:
            for cols in column_hits:
                if len(cols) == 1:
                    query.target = cols[0].table
                    break
        if query.target is None and query.aggregates:
            for agg in query.aggregates:
                if agg.column and agg.column.candidates:
                    query.target = agg.column.candidates[0].table
                    break
        if query.target is None:
            for spec in query.order_by:
                if spec.column is not None and len(spec.column.candidates) == 1:
                    query.target = spec.column.candidates[0].table
                    break
        if query.target is None and query.conditions:
            query.target = query.conditions[0].column.candidates[0].table
        if query.target is None:
            raise ParseError("could not tell which table the question is about")

    # -- finishing -----------------------------------------------------------------
    def _finalise(self, query: Query) -> None:
        if query.target:
            query.tables.insert(0, query.target)

        # A superlative with no explicit entity, asked as "what is ...", is a
        # value question: MAX/MIN rather than ORDER BY + LIMIT 1.
        if (
            "value_question" in query.intents
            and not query.aggregates
            and len(query.order_by) == 1
            and query.limit == 1
            and not query.projections
            and query.order_by[0].column is not None
        ):
            spec = query.order_by[0]
            func = "MAX" if spec.direction == "DESC" else "MIN"
            query.aggregates.append(AggregateSpec(func, spec.column, text=func.lower()))
            query.order_by.clear()
            query.limit = None
            query.intents.discard("top_k")

        if "existence" in query.intents and not query.aggregates:
            query.aggregates.append(AggregateSpec("COUNT", None, text="count"))

        # "which X have <condition on Y>" is a semi-join.
        condition_tables = {
            c.table
            for cond in query.conditions
            for c in cond.column.candidates
            if c.table
        }
        if (
            "entity_question" in query.intents
            and query.target
            and not query.aggregates
            and condition_tables
            and query.target not in condition_tables
        ):
            query.intents.add("semi_join")
            query.distinct = True

        if query.distinct:
            query.intents.add("distinct")

        for ref in query.projections:
            query.tables.extend(
                c.table for c in ref.candidates if c.table and len(ref.candidates) == 1
            )
        for cond in query.conditions:
            if len(cond.column.candidates) == 1:
                query.tables.append(cond.column.candidates[0].table or "")
        for spec in query.order_by:
            if spec.column and len(spec.column.candidates) == 1:
                query.tables.append(spec.column.candidates[0].table or "")
        for agg in query.aggregates:
            if agg.column and len(agg.column.candidates) == 1:
                query.tables.append(agg.column.candidates[0].table or "")
        for ref in query.group_by:
            if len(ref.candidates) == 1:
                query.tables.append(ref.candidates[0].table or "")

        query.tables = query.referenced_tables()


def parse(result: TagResult, question: str = "") -> Query:
    return Parser(result, question).parse()
