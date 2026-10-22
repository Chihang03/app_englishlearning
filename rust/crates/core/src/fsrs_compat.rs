//! Frozen py-fsrs 6.3.0 math with the app's empty learning/relearning steps.
//! Adapted under MIT; see rust/THIRD_PARTY_NOTICES.md. Not yet the full study transaction.
use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};
use std::f64::consts::E;
use thiserror::Error;

const W: [f64; 21] = [
    0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666, 0.796, 1.4835,
    0.0614, 0.2629, 1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542,
];

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Card {
    pub card_id: i64,
    pub state: u8,
    pub step: Option<i64>,
    pub stability: Option<f64>,
    pub difficulty: Option<f64>,
    pub due: DateTime<Utc>,
    pub last_review: Option<DateTime<Utc>>,
}

#[derive(Debug, Error)]
#[error("invalid FSRS card, rating, retention, or time range")]
pub struct InvalidCard;

fn decay() -> f64 {
    -W[20]
}
fn factor() -> f64 {
    0.9_f64.powf(1.0 / decay()) - 1.0
}
fn valid_number(n: f64) -> bool {
    n.is_finite() && n > 0.0
}
fn elapsed_days(now: DateTime<Utc>, last: DateTime<Utc>) -> i64 {
    // Python timedelta.days floors, including negative sub-day differences.
    let delta = now - last;
    let truncated = delta.num_days();
    if delta < Duration::days(truncated) {
        truncated - 1
    } else {
        truncated
    }
}

pub fn retrievability(card: &Card, now: DateTime<Utc>) -> Result<f64, InvalidCard> {
    let Some(last) = card.last_review else {
        return Ok(0.0);
    };
    let s = card
        .stability
        .filter(|s| valid_number(*s))
        .ok_or(InvalidCard)?;
    Ok((1.0 + factor() * elapsed_days(now, last).max(0) as f64 / s).powf(decay()))
}

pub fn review(
    card: &Card,
    rating: u8,
    now: DateTime<Utc>,
    retention: f64,
) -> Result<Card, InvalidCard> {
    if !(1..=4).contains(&rating)
        || !(1..=3).contains(&card.state)
        || !retention.is_finite()
        || retention <= 0.0
        || retention >= 1.0
    {
        return Err(InvalidCard);
    }
    let initial_d = |r: f64| W[4] - E.powf(W[5] * (r - 1.0)) + 1.0;
    let r = f64::from(rating);
    let initial = card.state == 1 && (card.stability.is_none() || card.difficulty.is_none());
    let (s, d) = if initial {
        (
            W[usize::from(rating - 1)].max(0.001),
            initial_d(r).clamp(1.0, 10.0),
        )
    } else {
        let s = card
            .stability
            .filter(|n| valid_number(*n))
            .ok_or(InvalidCard)?;
        let d = card
            .difficulty
            .filter(|n| n.is_finite() && (1.0..=10.0).contains(n))
            .ok_or(InvalidCard)?;
        let next_s = if card
            .last_review
            .is_some_and(|last| elapsed_days(now, last) < 1)
        {
            let mut increase = E.powf(W[17] * (r - 3.0 + W[18])) * s.powf(-W[19]);
            if rating >= 3 {
                increase = increase.max(1.0);
            }
            s * increase
        } else {
            let p = retrievability(card, now)?;
            if rating == 1 {
                let long = W[11]
                    * d.powf(-W[12])
                    * ((s + 1.0).powf(W[13]) - 1.0)
                    * E.powf((1.0 - p) * W[14]);
                long.min(s / E.powf(W[17] * W[18]))
            } else {
                let hard = if rating == 2 { W[15] } else { 1.0 };
                let easy = if rating == 4 { W[16] } else { 1.0 };
                s * (1.0
                    + E.powf(W[8])
                        * (11.0 - d)
                        * s.powf(-W[9])
                        * (E.powf((1.0 - p) * W[10]) - 1.0)
                        * hard
                        * easy)
            }
        };
        let delta = -(W[6] * (r - 3.0));
        let next_d = W[7] * initial_d(4.0) + (1.0 - W[7]) * (d + (10.0 - d) * delta / 9.0);
        (next_s.max(0.001), next_d.clamp(1.0, 10.0))
    };
    if !valid_number(s) || !valid_number(d) {
        return Err(InvalidCard);
    }
    let days = ((s / factor()) * (retention.powf(1.0 / decay()) - 1.0))
        .round_ties_even()
        .clamp(1.0, 365.0) as i64;
    let mut updated = card.clone();
    updated.state = 2;
    updated.step = None;
    updated.stability = Some(s);
    updated.difficulty = Some(d);
    updated.last_review = Some(now);
    updated.due = now
        .checked_add_signed(Duration::days(days))
        .ok_or(InvalidCard)?;
    Ok(updated)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn timedelta_days_floors_negative_microseconds() {
        let now: DateTime<Utc> = "2026-10-22T00:00:00Z".parse().unwrap();
        assert_eq!(elapsed_days(now, now + Duration::microseconds(1)), -1);
        assert_eq!(elapsed_days(now, now - Duration::hours(23)), 0);
        assert_eq!(elapsed_days(now, now - Duration::days(1)), 1);
    }
    #[derive(Deserialize)]
    struct Case {
        card: Card,
        rating: u8,
        now: DateTime<Utc>,
        retention: f64,
        expected: Card,
        recall: f64,
    }

    #[test]
    fn frozen_python_reference() {
        let cases: Vec<Case> =
            serde_json::from_str(include_str!("../../../fixtures/fsrs-v6.3.0.json")).unwrap();
        assert!(cases.len() >= 100);
        for c in cases {
            let actual = review(&c.card, c.rating, c.now, c.retention).unwrap();
            assert_eq!(
                (
                    actual.card_id,
                    actual.state,
                    actual.step,
                    actual.due,
                    actual.last_review
                ),
                (
                    c.expected.card_id,
                    c.expected.state,
                    c.expected.step,
                    c.expected.due,
                    c.expected.last_review
                )
            );
            for (a, b) in [
                (actual.stability.unwrap(), c.expected.stability.unwrap()),
                (actual.difficulty.unwrap(), c.expected.difficulty.unwrap()),
                (retrievability(&c.card, c.now).unwrap(), c.recall),
            ] {
                assert!((a - b).abs() <= 1e-12 * b.abs().max(1.0), "{} != {}", a, b);
            }
        }
    }
}
