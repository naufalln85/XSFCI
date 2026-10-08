"""Shared feature contract for GNN training and live inference."""

import json


FEATURE_PIPELINE_VERSION = "xfsci-gnn-23f-online-v5"

SERVICE_NAMES = (
    "frontend",
    "cartservice",
    "productcatalogservice",
    "redis-cart",
    "checkoutservice",
    "currencyservice",
    "emailservice",
    "shippingservice",
    "adservice",
    "recommendationservice",
    "paymentservice",
)


def extract_service_name(pod_name: str) -> str:
    """Map a Kubernetes pod name to one of the canonical service names."""
    pod_lower = str(pod_name).lower().strip()
    for service in SERVICE_NAMES:
        if pod_lower == service or pod_lower.startswith(f"{service}-"):
            return service
    return pod_lower

BASE_METRIC_COLS = [
    "cpu_usage", "memory_usage", "memory_usage_percent", "pod_restarts",
    "net_rx_bytes", "net_tx_bytes", "request_rate", "error_rate",
    "request_latency_p95_ms", "service_ready_ratio",
]

DERIVED_FEATURE_COLS = [
    "cpu_delta", "memory_delta", "memory_growth_rate",
    "cpu_rolling_mean_5", "cpu_rolling_std_5", "mem_rolling_mean_5",
    "restart_delta", "net_total_bytes", "net_rx_tx_ratio",
    "anomaly_score_raw", "memory_slope_12", "cpu_zscore_pod", "net_asymmetry",
]

MODEL_FEATURE_COLS = BASE_METRIC_COLS + DERIVED_FEATURE_COLS
APP_SPAN_SERVICES = (
    "checkoutservice",
    "currencyservice",
    "emailservice",
    "frontend",
    "paymentservice",
    "productcatalogservice",
    "recommendationservice",
)
# Services that have no server-span instrumentation in Online Boutique v0.10.1:
# - redis-cart: database without tracing
# - shippingservice: tracing disabled upstream (Go SDK issue #422)
# - cartservice: .NET binary in v0.10.1 has no OTLP trace exporter
# - adservice: Java image in v0.10.1 has no OpenTelemetry javaagent attached
APP_SPAN_ZERO_SERVICES = (
    "adservice",
    "cartservice",
    "redis-cart",
    "shippingservice",
)


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
        f'(rate(xfsci_calls_total{{{common}}}[2m]))'
    )
    errors = (
        'sum by (k8s_pod_name) '
        f'(rate(xfsci_calls_total{{{common},status_code=~"(?i).*error.*"}}[2m]))'
    )
    latency = (
        'histogram_quantile(0.95, '
        f'sum by (le, k8s_pod_name) '
        f'(rate(xfsci_duration_seconds_bucket{{{common}}}[2m])))'
    )
    return {
        "request_rate": total,
        # A pod with requests but no error spans has a measured 0 error rate.
        # Keep the total's pod labels when the error counter series is absent.
        "error_rate": (
            f'(({errors}) or (0 * ({total}))) '
            f'/ clamp_min(({total}), 1e-9)'
        ),
        # p95 server-span duration in milliseconds. Missing histograms are
        # handled as unknown when request volume is nonzero.
        "request_latency_p95_ms": f'({latency}) * 1000',
    }


def service_readiness_query(namespace: str) -> str:
    """Return a readiness ratio for Deployments and StatefulSets by service."""
    ns = json.dumps(str(namespace))
    deployments = (
        'sum by (deployment) '
        f'(kube_deployment_status_replicas_available{{namespace={ns}}}) '
        '/ clamp_min(sum by (deployment) '
        f'(kube_deployment_spec_replicas{{namespace={ns}}}), 1)'
    )
    statefulsets = (
        'sum by (statefulset) '
        f'(kube_statefulset_status_replicas_ready{{namespace={ns}}}) '
        '/ clamp_min(sum by (statefulset) '
        f'(kube_statefulset_replicas{{namespace={ns}}}), 1)'
    )
    return (
        f'label_replace(({deployments}), "service", "$1", "deployment", "(.+)") '
        f'or label_replace(({statefulsets}), "service", "$1", "statefulset", "(.+)")'
    )
