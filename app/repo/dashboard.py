"""Dashboard aggregation repository (Step 4, T4).

Computes pipeline metrics over non-terminal stages (item age in business days
via ``app/workdays.py::workdays_since``, since business days are not computed in
SQL) and a rollup of the ``llm_calls`` log. Read-only from the local DB.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from ..workdays import workdays_since


def stage_stats(conn: sqlite3.Connection, today: date) -> tuple[list[dict], int]:
    """Metrics per non-terminal stage plus the count of closed items.

    For each non-terminal stage (in ``position`` order), computes the item
    count and the current age in the stage in business days (avg rounded to 1
    decimal, max is an integer; both 0 for an empty stage). ``deals_closed`` is
    the number of items in terminal stages.
    """
    stage_rows = conn.execute(
        "SELECT id, name, position, is_terminal FROM stages ORDER BY position"
    ).fetchall()

    stages: list[dict] = []
    deals_closed = 0

    for stage in stage_rows:
        deal_rows = conn.execute(
            "SELECT stage_entered_at FROM deals WHERE stage_id = ?",
            (stage["id"],),
        ).fetchall()

        if stage["is_terminal"]:
            deals_closed += len(deal_rows)
            continue

        ages = [workdays_since(d["stage_entered_at"], today) for d in deal_rows]
        avg = round(sum(ages) / len(ages), 1) if ages else 0
        stages.append(
            {
                "stage_id": stage["id"],
                "name": stage["name"],
                "position": stage["position"],
                "deal_count": len(deal_rows),
                "avg_workdays_in_stage": avg,
                "max_workdays_in_stage": max(ages) if ages else 0,
            }
        )

    return stages, deals_closed


def llm_rollup(conn: sqlite3.Connection) -> dict:
    """Aggregation of ``llm_calls``: calls, errors, average time, token sums.

    Token sums cover all rows (errors have 0 tokens and do not distort the sum),
    as in ``app/repo/llm_calls.py``. An empty table yields zeros (no division by 0).
    """
    rows = conn.execute(
        "SELECT input_tokens, output_tokens, duration_ms, status FROM llm_calls"
    ).fetchall()

    total = len(rows)
    success = sum(1 for r in rows if r["status"] == "success")
    error = sum(1 for r in rows if r["status"] == "error")
    input_tokens = sum(r["input_tokens"] or 0 for r in rows)
    output_tokens = sum(r["output_tokens"] or 0 for r in rows)
    avg_duration = (
        round(sum(r["duration_ms"] or 0 for r in rows) / total) if total else 0
    )

    return {
        "total_calls": total,
        "success_calls": success,
        "error_calls": error,
        "avg_duration_ms": avg_duration,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
