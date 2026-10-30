//! Low-dimensional CAT with explicit expert priors; not a CEFR conversion.
use serde::{Deserialize, Serialize};
pub const MODEL: &str = "vocab-grid-v1";
pub const LABELS: [&str; 6] = [
    "基础词汇",
    "日常词汇",
    "常用进阶",
    "广泛阅读",
    "高阶词汇",
    "专业与低频词汇",
];
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Evidence {
    pub difficulty: f64,
    pub correct: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Estimate {
    pub mean: f64,
    pub sd: f64,
    pub low: f64,
    pub high: f64,
    pub stage: usize,
}
pub fn probability(theta: f64, difficulty: f64) -> f64 {
    0.25 + 0.75 / (1.0 + (difficulty - theta).exp())
}
pub fn estimate(evidence: &[Evidence]) -> Estimate {
    let grid: Vec<f64> = (0..=120).map(|i| -6.0 + f64::from(i) * 0.1).collect();
    let logs: Vec<f64> = grid
        .iter()
        .map(|theta| {
            let prior = -0.5 * (theta / 2.0).powi(2);
            prior
                + evidence
                    .iter()
                    .map(|e| {
                        let p = probability(*theta, e.difficulty).clamp(1e-8, 1.0 - 1e-8);
                        if e.correct { p.ln() } else { (1.0 - p).ln() }
                    })
                    .sum::<f64>()
        })
        .collect();
    let max = logs.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let weights: Vec<f64> = logs.iter().map(|x| (x - max).exp()).collect();
    let total: f64 = weights.iter().sum();
    let mean = grid.iter().zip(&weights).map(|(x, w)| x * w).sum::<f64>() / total;
    let sd = (grid
        .iter()
        .zip(&weights)
        .map(|(x, w)| (x - mean).powi(2) * w)
        .sum::<f64>()
        / total)
        .sqrt();
    let quantile = |q: f64| {
        let mut sum = 0.0;
        for (x, w) in grid.iter().zip(&weights) {
            sum += w / total;
            if sum >= q {
                return *x;
            }
        }
        6.0
    };
    Estimate {
        mean,
        sd,
        low: quantile(0.05),
        high: quantile(0.95),
        stage: ((mean + 2.5).round() as isize).clamp(0, 5) as usize,
    }
}
pub fn information(theta: f64, difficulty: f64) -> f64 {
    let sigmoid = 1.0 / (1.0 + (difficulty - theta).exp());
    let p = probability(theta, difficulty);
    let derivative = 0.75 * sigmoid * (1.0 - sigmoid);
    derivative.powi(2) / (p * (1.0 - p))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn extremes_are_finite_and_ordered() {
        let all = |correct| {
            estimate(
                &(0..36)
                    .map(|i| Evidence {
                        difficulty: f64::from(i % 6) - 2.5,
                        correct,
                    })
                    .collect::<Vec<_>>(),
            )
        };
        let low = all(false);
        let high = all(true);
        assert!(low.mean < -2.0 && high.mean > 2.0);
        assert!(low.sd.is_finite() && high.sd.is_finite());
        assert!(estimate(&[]).sd > low.sd);
    }
    #[test]
    fn repeated_evidence_reduces_uncertainty() {
        let es: Vec<_> = (0..36)
            .map(|i| Evidence {
                difficulty: f64::from(i % 6) - 2.5,
                correct: i % 6 < 3,
            })
            .collect();
        assert!(estimate(&es).sd < estimate(&es[..8]).sd);
    }
}
