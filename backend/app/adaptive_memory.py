"""FSRS memory estimates, a bounded prior for known words, and personal calibration.

The prior is a product policy, not a claim that one answer proves prior knowledge.
Calibration learns a scalar forgetting speed from genuinely delayed, unassisted
answers; it is evaluated chronologically before changing future intervals.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fsrs import Card, Rating, Scheduler, State

MODEL_VERSION = "fsrs-6.3.0-prior-v1"
TARGET_RETENTION = 0.9
MIN_CALIBRATION_SAMPLES = 150
CALIBRATION_BATCH = 50
MATURE_STABILITY_DAYS = 180
MATURE_AUDIT_DAYS = 180


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def scheduler(multiplier: float = 1.0) -> Scheduler:
    # Calibrated P(recall) = base_P(recall) ** multiplier. Solving for a
    # calibrated 90% target yields this target for the underlying FSRS model.
    return Scheduler(desired_retention=TARGET_RETENTION ** (1 / multiplier),
                     learning_steps=(), relearning_steps=(), maximum_interval=365,
                     enable_fuzzing=False)


def profile(conn, user_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM memory_profiles WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else {"forgetting_multiplier": 1.0, "evaluated_count": 0,
                                 "evaluated_at": None, "adopted": 0}


def log_loss(samples, multiplier: float) -> float:
    total = 0.0
    for probability, correct in samples:
        predicted = min(1 - 1e-6, max(1e-6, probability ** multiplier))
        total -= math.log(predicted if correct else 1 - predicted)
    return total / len(samples)


def fit_multiplier(training) -> float:
    # Regularize toward the default while keeping runtime training independent
    # of GPU/torch. The grid is a one-parameter calibration, not neural training.
    candidates = [0.5 + i * 0.025 for i in range(61)]
    return min(candidates, key=lambda value: log_loss(training, value) + 0.01 * math.log(value) ** 2)


def maybe_calibrate(conn, user_id: int, now: str) -> None:
    count = conn.execute("SELECT COUNT(*) FROM review_history WHERE user_id=? AND base_recall_probability IS NOT NULL",
                         (user_id,)).fetchone()[0]
    previous = profile(conn, user_id)
    if count < MIN_CALIBRATION_SAMPLES or count - previous["evaluated_count"] < CALIBRATION_BATCH:
        return
    rows = conn.execute("""SELECT base_recall_probability,is_correct,sense_id,review_time
        FROM review_history WHERE user_id=? AND base_recall_probability IS NOT NULL
        ORDER BY id DESC LIMIT 5000""", (user_id,)).fetchall()[::-1]
    # Several answers on one word or in one short session cannot establish a
    # personal long-term forgetting rate. These are conservative product gates.
    if len({r["sense_id"] for r in rows}) < 20 or (instant(rows[-1]["review_time"]) - instant(rows[0]["review_time"])).days < 30:
        return
    samples = [(r["base_recall_probability"], bool(r["is_correct"])) for r in rows]
    split = int(len(samples) * 0.8)
    training, validation = samples[:split], samples[split:]
    candidate = fit_multiplier(training)
    baseline_loss = log_loss(validation, 1.0)
    current_loss = log_loss(validation, previous["forgetting_multiplier"])
    candidate_loss = log_loss(validation, candidate)
    adopted = candidate_loss + 0.005 < min(baseline_loss, current_loss)
    selected = candidate if adopted else previous["forgetting_multiplier"]
    if not adopted and baseline_loss + 0.005 < current_loss:
        selected = 1.0
    conn.execute("""INSERT INTO memory_profiles(user_id,forgetting_multiplier,evaluated_count,
        training_count,validation_count,baseline_log_loss,candidate_log_loss,adopted,evaluated_at)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
        forgetting_multiplier=excluded.forgetting_multiplier,evaluated_count=excluded.evaluated_count,
        training_count=excluded.training_count,validation_count=excluded.validation_count,
        baseline_log_loss=excluded.baseline_log_loss,candidate_log_loss=excluded.candidate_log_loss,
        adopted=excluded.adopted,evaluated_at=excluded.evaluated_at""",
        (user_id,selected,count,len(training),len(validation),baseline_loss,candidate_loss,int(adopted),now))


def initial_card(conn, user_id: int, sense_id: int, current, now: datetime) -> Card:
    card = Card(card_id=sense_id)
    if current:
        # Historical answers did not reliably track pronunciation. Use the old
        # interval only as a conservative starting estimate, preserving due dates
        # until a real answer arrives. Do not invent independent confirmations.
        last = conn.execute("SELECT MAX(review_time) FROM review_history WHERE user_id=? AND sense_id=?",
                            (user_id,sense_id)).fetchone()[0]
        card = Card(card_id=sense_id, state=State.Review, step=None,
                    stability=max(1, min(365, current["interval_days"])), difficulty=5,
                    last_review=min(now, instant(last)) if last else now)
    return card


def advance_memory(conn, user_id: int, sense_id: int, current, *, correct: bool,
                   independent: bool, practice: bool, assisted: bool,
                   today: date, now: str, active_response_ms: int | None,
                   allow_known_prior: bool) -> dict[str, Any]:
    moment = instant(now)
    stored = conn.execute("SELECT * FROM adaptive_memory WHERE user_id=? AND sense_id=?",
                          (user_id,sense_id)).fetchone()
    card = Card.from_json(stored["card_json"]) if stored else initial_card(conn,user_id,sense_id,current,moment)
    personal = profile(conn, user_id)
    model = scheduler(personal["forgetting_multiplier"])
    base_probability = scheduler().get_card_retrievability(card,moment) if card.last_review else None
    eligible = (stored is not None and not practice and not assisted and card.last_review is not None
                and (moment - card.last_review).total_seconds() >= 86400)
    prediction = base_probability ** personal["forgetting_multiplier"] if eligible else None
    known = bool(stored["known_candidate"]) if stored else False
    confirmations = stored["confirmations"] if stored else 0
    first = stored["first_independent_at"] if stored else None
    last = stored["last_independent_at"] if stored else None

    if not practice:
        card, _ = model.review_card(card,Rating.Good if independent else Rating.Again,
                                    moment,review_duration=active_response_ms)
        if independent:
            if last is None or (moment - instant(last)).total_seconds() >= 86400:
                confirmations += 1
                first = first or now
                last = now
            if stored is None and current is None:
                # Only brand-new history recorded with v7 hint tracking qualifies.
                historical = conn.execute("SELECT 1 FROM review_history WHERE user_id=? AND sense_id=? LIMIT 1",
                                          (user_id,sense_id)).fetchone()
                exposed = conn.execute("SELECT 1 FROM sense_exposures WHERE user_id=? AND sense_id=?",
                                       (user_id,sense_id)).fetchone()
                known = historical is None and exposed is None and allow_known_prior
                if known:
                    card.stability = 30.0
        else:
            known = False
            confirmations = 0
            first = last = None
    elif assisted:
        # Assistance is not a supervised failure. Lower confidence conservatively
        # without including it among calibration labels or successful recalls.
        card.stability = min(card.stability or 1, 3)
        card.last_review = moment
        known = False
        confirmations = 0
        first = last = None

    # Recompute due after initializing the known-word prior. Personal retention
    # affects scheduling, while the stored FSRS stability retains its definition.
    # Use the public recall API to solve the interval, rather than relying on
    # private library formulas. Round to the app's calendar-day scheduling unit.
    probe = Card(state=State.Review,stability=card.stability or 1,last_review=moment)
    low, high = 0.0, 365.0
    for _ in range(18):
        middle = (low + high) / 2
        if model.get_card_retrievability(probe,moment + timedelta(days=middle)) > model.desired_retention:
            low = middle
        else:
            high = middle
    interval = max(1, round(high))
    if known and confirmations < 3:
        interval = min(interval,30 if confirmations == 1 else 60)
    mature = (independent and confirmations >= 3 and card.stability >= MATURE_STABILITY_DAYS
              and interval >= MATURE_AUDIT_DAYS
              and first is not None and (moment - instant(first)).total_seconds() >= 90 * 86400)
    if mature:
        interval = MATURE_AUDIT_DAYS
    card.due = moment + timedelta(days=interval)
    conn.execute("""INSERT INTO adaptive_memory(user_id,sense_id,card_json,known_candidate,
        confirmations,first_independent_at,last_independent_at) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(user_id,sense_id) DO UPDATE SET card_json=excluded.card_json,
        known_candidate=excluded.known_candidate,confirmations=excluded.confirmations,
        first_independent_at=excluded.first_independent_at,last_independent_at=excluded.last_independent_at""",
        (user_id,sense_id,card.to_json(),int(known),confirmations,first,last))
    return {"interval_days": interval, "next_review_date": (today + timedelta(days=interval)).isoformat(),
            "mature": mature, "known_candidate": known, "confirmations": confirmations,
            "stability_days": round(card.stability or 0,2), "difficulty": round(card.difficulty or 0,2),
            "base_recall_probability": base_probability if eligible else None,
            "predicted_recall_probability": prediction, "model_version": MODEL_VERSION,
            "target_retention": TARGET_RETENTION,
            "personalized": personal["forgetting_multiplier"] != 1.0}


def memory_summary(conn, user_id: int) -> dict[str, Any]:
    personal = profile(conn,user_id)
    count = conn.execute("SELECT COUNT(*) FROM review_history WHERE user_id=? AND base_recall_probability IS NOT NULL",
                         (user_id,)).fetchone()[0]
    return {"model": MODEL_VERSION,"target_retention": TARGET_RETENTION,
            "personalized": personal["forgetting_multiplier"] != 1.0,
            "sample_count": count,"minimum_samples": MIN_CALIBRATION_SAMPLES,
            "forgetting_multiplier": personal["forgetting_multiplier"],
            "evaluated_at": personal["evaluated_at"],
            "training_count": personal.get("training_count",0),
            "validation_count": personal.get("validation_count",0),
            "baseline_log_loss": personal.get("baseline_log_loss"),
            "candidate_log_loss": personal.get("candidate_log_loss")}
