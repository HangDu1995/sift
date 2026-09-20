"""Run duration anomaly detection utilities."""

from __future__ import annotations


def detect_outliers(
    durations: list[dict],
    threshold_multiplier: float = 3.0,
) -> list[dict]:
    """Detect outlier runs based on duration using Median Absolute Deviation.

    Each dict: {eval_id, config, run_number, duration_seconds}
    Returns list of dicts with anomaly_reason added.
    """
    if len(durations) < 4:
        return []

    import statistics

    values = [d["duration_seconds"] for d in durations]
    median = statistics.median(values)
    deviations = [abs(v - median) for v in values]
    mad = statistics.median(deviations) if deviations else 0.0

    if mad == 0:
        return []

    anomalies = []
    for d in durations:
        if not (d["duration_seconds"] > median + threshold_multiplier * mad):
            continue
        anomalies.append(
            {
                **d,
                "anomaly_type": "outlier",
                "detail": (
                    "Duration "
                    f"{d['duration_seconds']:.1f}"
                    "s > median "
                    f"{median:.1f}"
                    "s + "
                    f"{threshold_multiplier}"
                    "×MAD "
                    f"{threshold_multiplier * mad:.1f}"
                    "s"
                ),
            }
        )
    return anomalies


def detect_timeout(
    durations: list[dict],
    timeout_seconds: int,
) -> list[dict]:
    """Detect runs that exceeded the per-eval timeout."""
    anomalies = []
    for d in durations:
        if not (d["duration_seconds"] > timeout_seconds):
            continue
        anomalies.append(
            {
                **d,
                "anomaly_type": "timeout",
                "detail": (f"Duration {d['duration_seconds']:.1f}s > timeout {timeout_seconds}s"),
            }
        )
    return anomalies
