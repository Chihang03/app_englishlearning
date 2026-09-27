from __future__ import annotations

from datetime import date, timedelta
from typing import Any


INITIAL_INTERVALS = [1, 3, 7, 15, 30, 90, 180]
RELEARNING_DELAY_SECONDS = 20 * 60


def next_state(
    current: dict[str, Any] | None,
    is_correct: bool,
    had_wrong_in_last_180_days: bool,
    today: date,
) -> dict[str, Any]:
    """Advance a word's schedule.

    `today` is the caller's calendar day, which is resolved in the learner's own
    timezone rather than the server's, so due dates line up with their days.
    """
    state = {
        "review_count": 0,
        "correct_count": 0,
        "wrong_count": 0,
        "lapse_count": 0,
        "easiness_factor": 2.5,
        "interval_days": 0,
        "next_review_date": today.isoformat(),
        "status": "New",
    }
    if current:
        state.update(current)

    state["review_count"] += 1

    if not is_correct:
        state["correct_count"] = 0
        state["wrong_count"] += 1
        state["lapse_count"] += 1
        state["easiness_factor"] = max(1.3, round(float(state["easiness_factor"]) - 0.2, 2))
        state["interval_days"] = 0
        state["next_review_date"] = today.isoformat()
        state["status"] = "Learning"
        return state

    state["correct_count"] += 1
    state["easiness_factor"] = min(3.0, round(float(state["easiness_factor"]) + 0.08, 2))

    correct_streak = int(state["correct_count"])
    if correct_streak <= len(INITIAL_INTERVALS):
        interval_days = INITIAL_INTERVALS[correct_streak - 1]
    else:
        interval_days = max(
            int(round(int(state["interval_days"]) * float(state["easiness_factor"]))),
            int(state["interval_days"]) + 1,
        )

    state["interval_days"] = interval_days
    state["next_review_date"] = (today + timedelta(days=interval_days)).isoformat()
    state["status"] = "Mature" if correct_streak >= 8 and not had_wrong_in_last_180_days else "Reviewing"
    return state
