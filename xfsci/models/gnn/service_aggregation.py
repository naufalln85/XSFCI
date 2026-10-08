"""Shared replica-to-service aggregation for offline training and live input."""

from __future__ import annotations

import numpy as np
import pandas as pd


_SUM_FEATURES = {"net_rx_bytes", "net_tx_bytes", "request_rate"}


def aggregate_service_rows(rows: pd.DataFrame, feature_columns: list[str]) -> np.ndarray:
    """Aggregate replicas without averaging away a single affected replica.

    Throughput counters/rates are summed, error fraction is request-weighted,
    and other features use the worst observed replica. The caller must pass
    only telemetry-complete replicas when such a mask is available.
    """
    if rows.empty:
        raise ValueError("cannot aggregate an empty service group")

    result: list[float] = []
    request_rate = pd.to_numeric(rows.get("request_rate", 0.0), errors="coerce")
    if not isinstance(request_rate, pd.Series):
        request_rate = pd.Series(float(request_rate), index=rows.index)
    request_rate = request_rate.fillna(0.0).clip(lower=0.0)

    for column in feature_columns:
        base = column.removesuffix("_norm")
        values = pd.to_numeric(rows[column], errors="coerce").replace(
            [np.inf, -np.inf], np.nan
        ).dropna()
        if values.empty:
            result.append(0.0)
        elif base in _SUM_FEATURES:
            result.append(float(values.sum()))
        elif base == "error_rate" and "error_rate" in rows:
            error = values.reindex(rows.index)
            valid = error.notna() & request_rate.reindex(error.index).gt(0)
            if valid.any():
                result.append(float(np.average(error[valid], weights=request_rate[valid])))
            else:
                result.append(float(error.max()))
        else:
            result.append(float(values.max()))
    aggregated = dict(zip(feature_columns, result))
    rx = aggregated.get("net_rx_bytes")
    tx = aggregated.get("net_tx_bytes")
    if rx is not None and tx is not None:
        total = float(rx) + float(tx)
        if "net_total_bytes" in aggregated:
            aggregated["net_total_bytes"] = total
        if "net_rx_tx_ratio" in aggregated:
            aggregated["net_rx_tx_ratio"] = min(100.0, float(rx) / (float(tx) + 1e-10))
        if "net_asymmetry" in aggregated:
            aggregated["net_asymmetry"] = abs(float(rx) - float(tx)) / (total + 1e-10)
    return np.asarray([aggregated[column] for column in feature_columns], dtype=np.float32)
