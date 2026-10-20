"""Gemini fallback for questions outside the controlled grammar.

The fallback never raises: no key, no network, a quota error or an empty
response all produce a clearly-marked ``fallback_unavailable`` result. The
API key is read from the environment only, and is never logged or cached.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CACHE_DIR = Path(os.environ.get("NL2SQL_CACHE_DIR", Path.cwd() / ".cache" / "llm"))
MODEL_CACHE = CACHE_DIR / "model.json"
REQUEST_TIMEOUT = 30.0

PROMPT = """You translate questions into SQL for SQLite.

Schema:
{schema}

Question: {question}

Return exactly one SQLite SELECT statement that answers the question.
Use only the tables and columns listed above. No prose, no explanation,
no comments, no trailing semicolon. Do not modify any data."""

_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.S | re.I)

# Model names that are not a good default for this job.
_MODEL_EXCLUDE = ("lite", "preview", "exp", "tts", "image", "live", "audio",
                  "thinking", "vision", "embedding")


@dataclass
class LLMResponse:
    sql: str = ""
    model: str = ""
    cached: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.sql) and not self.error


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(override=False)


def api_key() -> str:
    _load_env()
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _version_key(name: str) -> tuple:
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", name)]
    return (numbers or [0.0], name)


def resolve_model(client: Any) -> str:
    """Use ``GEMINI_MODEL`` if set, else pick the newest stable Flash model."""
    _load_env()
    configured = os.environ.get("GEMINI_MODEL", "").strip()
    if configured:
        return configured
    try:
        cached = json.loads(MODEL_CACHE.read_text(encoding="utf-8"))
        if cached.get("model"):
            return str(cached["model"])
    except (OSError, ValueError):
        pass

    names: list[str] = []
    try:
        for model in client.models.list():
            name = str(getattr(model, "name", "") or "").split("/")[-1]
            actions = getattr(model, "supported_actions", None) or getattr(
                model, "supported_generation_methods", None
            ) or []
            if actions and "generateContent" not in actions:
                continue
            if name:
                names.append(name)
    except Exception:
        return "gemini-2.5-flash"

    flash = [n for n in names if "flash" in n.lower()]
    stable = [n for n in flash if not any(bad in n.lower() for bad in _MODEL_EXCLUDE)]
    pool = stable or [n for n in flash if "lite" not in n.lower()] or flash
    if not pool:
        return "gemini-2.5-flash"
    chosen = sorted(pool, key=_version_key)[-1]
    _write_json(MODEL_CACHE, {"model": chosen, "resolved_at": time.time()})
    return chosen


def _write_json(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass


def normalize_question(question: str) -> str:
    return " ".join(question.lower().split())


def cache_path(schema_hash: str, question: str, model: str) -> Path:
    key = f"{schema_hash}|{normalize_question(question)}|{model}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def read_cache(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def extract_sql(text: str) -> str:
    """Pull a single statement out of a response that may be fenced."""
    if not text:
        return ""
    match = _FENCE.search(text)
    body = match.group(1) if match else text
    body = body.strip()
    if body.lower().startswith("sql\n"):
        body = body[4:]
    return body.strip().rstrip(";").strip()


def build_prompt(db, question: str) -> str:
    return PROMPT.format(schema=db.symbols.compact_description(5), question=question)


def _make_client(key: str):
    from google import genai

    return genai.Client(api_key=key)


def ask_model(db, question: str, client: Any | None = None) -> LLMResponse:
    """Ask the model for one SELECT, using the on-disk cache when possible."""
    key = api_key()
    if client is None and not key:
        return LLMResponse(error="no GEMINI_API_KEY is configured")

    try:
        if client is None:
            client = _make_client(key)
    except ImportError as exc:
        return LLMResponse(error=f"google-genai is not installed ({exc})")
    except Exception as exc:
        return LLMResponse(error=f"could not create the client: {exc}")

    try:
        model = resolve_model(client)
    except Exception as exc:
        return LLMResponse(error=f"could not resolve a model: {exc}")

    path = cache_path(db.symbols.schema_hash, question, model)
    cached = read_cache(path)
    if cached and cached.get("sql"):
        return LLMResponse(sql=cached["sql"], model=cached.get("model", model),
                           cached=True)

    prompt = build_prompt(db, question)
    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config={"temperature": 0},
        )
        text = getattr(response, "text", "") or ""
    except Exception as exc:
        return LLMResponse(model=model, error=f"the model call failed: {exc}")

    sql = extract_sql(text)
    if not sql:
        return LLMResponse(model=model, error="the model returned no SQL")

    _write_json(path, {"sql": sql, "model": model, "raw": text,
                       "question": question, "at": time.time()})
    return LLMResponse(sql=sql, model=model)


def run_fallback(question, db, result, dialect: str = "sqlite", execute: bool = True,
                 client: Any | None = None):
    """Fill ``result`` from the model, validating and optimizing what comes back."""
    from . import codegen, equivalence
    from .optimizer import optimize
    from .semantic import Diagnostic
    from .sql_to_ir import Unsupported, convert

    answer = ask_model(db, question, client=client)
    result.llm_model = answer.model
    if not answer.ok:
        result.status = "fallback_unavailable"
        result.diagnostics.append(
            Diagnostic("warning", "fallback-unavailable", answer.error)
        )
        return result
    if answer.cached:
        result.diagnostics.append(
            Diagnostic("info", "llm-cache", "answered from the on-disk cache")
        )

    ok, why = codegen.is_single_select(answer.sql)
    if not ok:
        result.status = "error"
        result.diagnostics.append(Diagnostic("error", "unsafe-sql", why))
        return result

    try:
        result.naive_sql = codegen.to_dialect(answer.sql, "sqlite")
    except Exception as exc:
        result.status = "error"
        result.diagnostics.append(
            Diagnostic("error", "llm-sql", f"the model's SQL did not parse: {exc}")
        )
        return result

    try:
        plan = convert(result.naive_sql, db.symbols)
    except Unsupported as exc:
        result.optimizable = False
        result.plan = None
        result.optimized_sql = result.naive_sql
        result.dialect_sql = _safe_dialect(result.naive_sql, dialect, result)
        result.diagnostics.append(
            Diagnostic("info", "llm-unoptimized",
                       f"kept as written: {exc}")
        )
        if execute:
            _execute_only(result, db)
        return result
    except Exception as exc:
        result.optimizable = False
        result.optimized_sql = result.naive_sql
        result.dialect_sql = _safe_dialect(result.naive_sql, dialect, result)
        result.diagnostics.append(
            Diagnostic("warning", "llm-convert", f"could not build an IR: {exc}")
        )
        if execute:
            _execute_only(result, db)
        return result

    result.plan = plan
    opt = optimize(plan, db.symbols)
    result.optimized_plan = opt.plan
    result.trace = opt.trace
    result.skipped_rules = opt.skipped
    try:
        result.optimized_sql = codegen.generate(opt.plan, "sqlite")
    except Exception as exc:
        result.optimized_sql = result.naive_sql
        result.diagnostics.append(
            Diagnostic("warning", "codegen", f"kept the model's SQL: {exc}")
        )
    result.dialect_sql = _safe_dialect(result.optimized_sql, dialect, result)

    if execute:
        from .pipeline import _execute

        _execute(result, db)
    return result


def _safe_dialect(sql: str, dialect: str, result) -> str:
    from . import codegen
    from .semantic import Diagnostic

    if dialect == "sqlite":
        return sql
    try:
        return codegen.to_dialect(sql, dialect)
    except Exception as exc:
        result.diagnostics.append(
            Diagnostic("warning", "dialect", f"cannot render {dialect}: {exc}")
        )
        return sql


def _execute_only(result, db) -> None:
    """Run SQL we could not model, to validate it; equivalence is not applicable."""
    from . import equivalence
    from .codegen import explain_plan
    from .semantic import Diagnostic

    conn = db.connect()
    try:
        start = time.perf_counter()
        result.columns, rows = equivalence.run_query(conn, result.optimized_sql)
        result.rows = rows[:1000]
        result.timings["execute"] = (time.perf_counter() - start) * 1000.0
        result.plan_before = explain_plan(conn, result.optimized_sql)
        result.plan_after = result.plan_before
        result.equivalence = equivalence.not_applicable("validated by execution")
    except Exception as exc:
        result.status = "error"
        result.diagnostics.append(Diagnostic("error", "execution", str(exc)))
    finally:
        conn.close()
