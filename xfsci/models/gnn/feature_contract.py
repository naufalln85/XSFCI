"""Shared feature contract for GNN training and live inference."""

FEATURE_PIPELINE_VERSION = "xfsci-gnn-21f-online-v2"

BASE_METRIC_COLS = [
    "cpu_usage", "memory_usage", "memory_usage_percent", "pod_restarts",
    "net_rx_bytes", "net_tx_bytes", "request_rate", "error_rate",
]

DERIVED_FEATURE_COLS = [
    "cpu_delta", "memory_delta", "memory_growth_rate",
    "cpu_rolling_mean_5", "cpu_rolling_std_5", "mem_rolling_mean_5",
    "restart_delta", "net_total_bytes", "net_rx_tx_ratio",
    "anomaly_score_raw", "memory_slope_12", "cpu_zscore_pod", "net_asymmetry",
]

MODEL_FEATURE_COLS = BASE_METRIC_COLS + DERIVED_FEATURE_COLS
