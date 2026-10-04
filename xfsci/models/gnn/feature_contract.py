"""Shared feature contract for GNN training and live inference."""

import json


FEATURE_PIPELINE_VERSION = "xfsci-gnn-21f-online-v3"

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
APP_SPAN_SERVICES = (
    "adservice",
    "cartservice",
    "checkoutservice",
    "currencyservice",
    "emailservice",
    "frontend",
    "paymentservice",
    "productcatalogservice",
    "recommendationservice",
)
# Services that are expected to produce zero trace-derived metrics.
# shippingservice: upstream Go SDK has tracing disabled (issue #422).
APP_SPAN_ZERO_SERVICES = ("redis-cart", "shippingservice")


def application_span_selector(namespace: str, pod_name_regex: str | None = None) -> str:
    """Build the shared PromQL selector for instrumented server spans.

    Health-check span names (Health/Check, grpc.health.*) are excluded at query
    time as defense-in-depth; the Collector filter/health_checks processor is
    the primary exclusion layer.
    """
    labels = [
        f'k8s_namespace_name={json.dumps(str(namespace))}',
        'span_kind="SPAN_KIND_SERVER"',
        'span_name!~".*Health/Check.*|grpc\\\\.health\\\\..*|_healthz"',
    ]
    if pod_name_regex:
        labels.append(f'k8s_pod_name=~{json.dumps(pod_name_regex)}')
    return ",".join(labels)


def application_metric_queries(
    namespace: str,
    pod_name_regex: str | None = None,
) -> dict[str, str]:
    """Return the canonical trace-derived request metrics used by train and live.

    The OpenTelemetry span-metrics connector exports server-span counts as
    ``xfsci_calls_total`` with Kubernetes pod and namespace dimensions. Error
    fraction is based on spans with OTel status Error; it stays in [0, 1].
    """
    common = application_span_selector(namespace, pod_name_regex)
    total = (
        'sum by (k8s_pod_name) '
        f'(rate(xfsci_calls_total{{{common}}}[1m]))'
    )
    errors = (
        'sum by (k8s_pod_name) '
        f'(rate(xfsci_calls_total{{{common},status_code=~"(?i).*error.*"}}[1m]))'
    )
    return {
        "request_rate": total,
        # A pod with requests but no error spans has a measured 0 error rate.
        # Keep the total's pod labels when the error counter series is absent.
        "error_rate": (
            f'(({errors}) or (0 * ({total}))) '
            f'/ clamp_min(({total}), 1e-9)'
        ),
    }
