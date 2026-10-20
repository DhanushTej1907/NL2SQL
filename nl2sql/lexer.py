"""Tokenisation: spaCy tokens plus normalised comparison phrases.

The lexer knows English, not the database. Grammar words ("how many", "per",
"more than") are hard-coded on purpose; schema words never are.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_NLP = None
_NLP_NAME = ""
_HAS_VECTORS = False

# Multi-word comparison phrases, longest first so "no more than" beats
# "more than".
COMPARISON_PHRASES: list[tuple[tuple[str, ...], str]] = [
    (("no", "more", "than"), "<="),
    (("no", "less", "than"), ">="),
    (("not", "greater", "than"), "<="),
    (("not", "less", "than"), ">="),
    (("greater", "than", "or", "equal", "to"), ">="),
    (("less", "than", "or", "equal", "to"), "<="),
    (("at", "least"), ">="),
    (("at", "most"), "<="),
    (("more", "than"), ">"),
    (("greater", "than"), ">"),
    (("larger", "than"), ">"),
    (("higher", "than"), ">"),
    (("bigger", "than"), ">"),
    (("less", "than"), "<"),
    (("fewer", "than"), "<"),
    (("lower", "than"), "<"),
    (("smaller", "than"), "<"),
    (("equal", "to"), "="),
    (("not", "equal", "to"), "!="),
    (("other", "than"), "!="),
    (("exceeding",), ">"),
    (("exceeds",), ">"),
    (("above",), ">"),
    (("over",), ">"),
    (("under",), "<"),
    (("below",), "<"),
    (("beneath",), "<"),
    (("equals",), "="),
]

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100,
    "thousand": 1000,
}

_QUOTED = re.compile(r"\"([^\"]+)\"|'([^']+)'")


def load_nlp():
    """Load spaCy once, preferring the vector model."""
    global _NLP, _NLP_NAME, _HAS_VECTORS
    if _NLP is not None:
        return _NLP
    import spacy

    for name in ("en_core_web_md", "en_core_web_sm"):
        try:
            _NLP = spacy.load(name, disable=["ner", "parser"])
            _NLP_NAME = name
            break
        except OSError:
            continue
    if _NLP is None:
        _NLP = spacy.blank("en")
        _NLP_NAME = "blank:en"
    _HAS_VECTORS = bool(getattr(_NLP, "vocab", None)) and _NLP.vocab.vectors_length > 0
    return _NLP


def model_info() -> tuple[str, bool]:
    load_nlp()
    return _NLP_NAME, _HAS_VECTORS


def has_vectors() -> bool:
    load_nlp()
    return _HAS_VECTORS


@dataclass
class Token:
    text: str
    lower: str
    lemma: str
    pos: str
    index: int
    is_num: bool = False
    num_value: float | None = None
    is_quoted: bool = False
    operator: str | None = None      # set for normalised comparison phrases
    span: tuple[int, int] = (0, 0)   # character offsets in the question
    source_indices: tuple[int, ...] = ()

    @property
    def is_operator(self) -> bool:
        return self.operator is not None

    def value(self) -> Any:
        if self.is_num and self.num_value is not None:
            if float(self.num_value).is_integer():
                return int(self.num_value)
            return self.num_value
        return self.text


@dataclass
class LexResult:
    question: str
    tokens: list[Token] = field(default_factory=list)
    model: str = ""
    vectors: bool = False

    def texts(self) -> list[str]:
        return [t.lower for t in self.tokens]


def _parse_number(text: str) -> float | None:
    cleaned = text.replace(",", "").replace("$", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return NUMBER_WORDS.get(cleaned.lower())


def tokenize(question: str) -> LexResult:
    """Turn a question into tokens with comparison phrases collapsed."""
    nlp = load_nlp()
    name, vectors = model_info()

    # Protect quoted literals from being split by the tokenizer.
    quoted_spans: list[tuple[int, int, str]] = []
    for match in _QUOTED.finditer(question):
        literal = match.group(1) if match.group(1) is not None else match.group(2)
        quoted_spans.append((match.start(), match.end(), literal))

    doc = nlp(question)
    raw: list[Token] = []
    for i, tok in enumerate(doc):
        if tok.is_space or (tok.is_punct and tok.text not in ("=", ">", "<", ">=", "<=")):
            continue
        lower = tok.text.lower()
        num = _parse_number(tok.text) if (tok.like_num or lower in NUMBER_WORDS) else None
        quoted = any(s <= tok.idx < e for s, e, _ in quoted_spans)
        raw.append(
            Token(
                text=tok.text,
                lower=lower,
                lemma=(tok.lemma_ or lower).lower(),
                pos=tok.pos_ or "",
                index=i,
                is_num=num is not None,
                num_value=num,
                is_quoted=quoted,
                span=(tok.idx, tok.idx + len(tok.text)),
                source_indices=(i,),
            )
        )

    tokens = _collapse_operators(raw)
    tokens = _collapse_symbols(tokens)
    for n, tok in enumerate(tokens):
        tok.index = n
    return LexResult(question=question, tokens=tokens, model=name, vectors=vectors)


def _collapse_operators(tokens: list[Token]) -> list[Token]:
    out: list[Token] = []
    i = 0
    phrases = sorted(COMPARISON_PHRASES, key=lambda p: -len(p[0]))
    while i < len(tokens):
        matched = False
        for words, op in phrases:
            n = len(words)
            if i + n > len(tokens):
                continue
            window = tuple(t.lower for t in tokens[i : i + n])
            lemmas = tuple(t.lemma for t in tokens[i : i + n])
            if window == words or lemmas == words:
                span = (tokens[i].span[0], tokens[i + n - 1].span[1])
                out.append(
                    Token(
                        text=" ".join(t.text for t in tokens[i : i + n]),
                        lower=" ".join(window),
                        lemma=" ".join(window),
                        pos="OP",
                        index=len(out),
                        operator=op,
                        span=span,
                        source_indices=tuple(
                            idx for t in tokens[i : i + n] for idx in t.source_indices
                        ),
                    )
                )
                i += n
                matched = True
                break
        if not matched:
            out.append(tokens[i])
            i += 1
    return out


_SYMBOL_OPS = {">": ">", "<": "<", "=": "=", ">=": ">=", "<=": "<=", "!=": "!=", "<>": "!="}


def _collapse_symbols(tokens: list[Token]) -> list[Token]:
    for tok in tokens:
        if tok.operator is None and tok.text in _SYMBOL_OPS:
            tok.operator = _SYMBOL_OPS[tok.text]
            tok.pos = "OP"
    return tokens
