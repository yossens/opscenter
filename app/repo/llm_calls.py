"""Gemini call-log aggregation repository (Step 3, T5).

Computes cost statistics over the ``llm_calls`` table across two windows:
"today" and "last 30 days". Window boundaries use the LOCAL calendar date,
while ``llm_calls.created_at`` is stored in UTC ISO — so each timestamp is
converted to a local date (``workdays._to_local_date``: ``.astimezone().date()``)
BEFORE bucketing, to avoid an off-by-one around midnight.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .. import config
from ..workdays import _to_local_date


def _empty_window() -> dict:
    return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


def _cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1e6 * config.LLM_PRICE_INPUT_PER_1M
        + output_tokens / 1e6 * config.LLM_PRICE_OUTPUT_PER_1M
    )


def get_stats(conn: sqlite3.Connection) -> dict:
    """LLM cost statistics for "today" and "last 30 days".

    ``calls`` counts all rows in the window (including ``status='error'``); token
    sums come from the stored columns (errors have 0 tokens and do not distort
    the sum). An empty table yields zeros (no crash).
    """
    rows = conn.execute(
        "SELECT created_at, input_tokens, output_tokens FROM llm_calls"
    ).fetchall()

    today_local = date.today()
    window_start_30 = today_local - timedelta(days=30)

    today = _empty_window()
    last_30 = _empty_window()

    for row in rows:
        local_date = _to_local_date(row["created_at"])
        input_tokens = row["input_tokens"] or 0
        output_tokens = row["output_tokens"] or 0

        if local_date >= window_start_30:
            last_30["calls"] += 1
            last_30["input_tokens"] += input_tokens
            last_30["output_tokens"] += output_tokens

        if local_date == today_local:
            today["calls"] += 1
            today["input_tokens"] += input_tokens
            today["output_tokens"] += output_tokens

    for window in (today, last_30):
        window["cost_usd"] = _cost_usd(window["input_tokens"], window["output_tokens"])

    return {"today": today, "last_30_days": last_30}
