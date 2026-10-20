"""Gemini fallback (filled in at M5)."""

from __future__ import annotations


def run_fallback(question, db, result, dialect="sqlite", execute=True):
    result.status = "fallback_unavailable"
    return result
