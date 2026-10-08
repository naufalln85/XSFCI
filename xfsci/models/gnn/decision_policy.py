"""Shared deterministic physical guardrails for offline evaluation and live inference."""

from __future__ import annotations

import numpy as np


def apply_physical_guardrails(
    probabilities: np.ndarray,
    features: np.ndarray,
    feature_index: dict[str, int],
) -> np.ndarray:
    """Suppress fault classes only when their observable evidence is absent.

    Values are the same min-max normalized values supplied to the GNN. The
    caller handles the separate graph-risk threshold.
    """
    result = np.asarray(probabilities, dtype=np.float64).copy()
    row = np.asarray(features, dtype=np.float64)

    if (
        row[feature_index["pod_restarts_norm"]] <= 0.0
        and row[feature_index["restart_delta_norm"]] <= 0.0
        and row[feature_index["service_ready_ratio_norm"]] >= 0.999
    ):
        result[3] = 0.0
    if row[feature_index["cpu_usage_norm"]] < 0.05:
        result[1] = 0.0
    if row[feature_index["memory_slope_12_norm"]] <= 0.01:
        result[2] = 0.0
    # The contract uses fixed 0..2000 ms bounds; 0.05 corresponds to 100 ms,
    # matching the minimum latency floor used by the fault labeler.
    if row[feature_index["request_latency_p95_ms_norm"]] < 0.05:
        result[4] = 0.0
    return result
