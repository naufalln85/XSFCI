# Application request telemetry

The Online Boutique images in `workloads/online-boutique.yaml` do not expose the
Prometheus endpoint assumed by the old pod annotations. Application request
telemetry now comes from OTLP traces, converted to server-span count, error, and
duration metrics by the OpenTelemetry Collector span metrics connector.

## Deployment order

Run from the repository root, after updating the VM checkout:

```bash
kubectl apply -f infrastructure/monitoring/otel-collector.yaml
kubectl rollout status deployment/otel-collector -n demo --timeout=180s
kubectl apply -f infrastructure/workloads/online-boutique.yaml
```

Applying the workload manifest rolls the ten application deployments so they
can send traces to the Collector. The Collector Role can read pod metadata only
in namespace `demo`. Apply this in the demo environment first: the upstream
frontend tracer uses always-on sampling, so trace volume follows request volume.

## Validate telemetry

Wait at least one minute and exercise a checkout flow so all server services
receive traffic. Then query Prometheus:

```bash
PROM=http://172.20.0.104:30090

curl -fsSG "$PROM/api/v1/query" --data-urlencode \
  'query=count by (k8s_pod_name) (xfsci_calls_total{k8s_namespace_name="demo",span_kind="SPAN_KIND_SERVER"})'

curl -fsSG "$PROM/api/v1/query" --data-urlencode \
  'query=up{job="kubernetes-pods",kubernetes_namespace="demo"}'
```

The first query should show traced application pods. The second should show the
Collector scrape target as healthy; application pods are no longer annotated as
Prometheus scrape targets.

## Model compatibility

`error_rate` means the fraction of server spans with OpenTelemetry status
`Error`; it is not the former HTTP 4xx/5xx ratio. The feature contract also uses
a two-minute request-rate window and records four services without server-span
instrumentation as explicit zero-valued trace features. These semantics are
versioned as `xfsci-gnn-23f-online-v5`. Recollect and label training data,
regenerate the feature contract and scaler, and retrain the GNN before enabling
inference. The feature-version guard rejects checkpoints and preprocessing
artifacts from any earlier pipeline version.

`adservice`, `cartservice`, `redis-cart`, and `shippingservice` currently have no
server-span instrumentation in this Online Boutique v0.10.1 deployment. Their
`request_rate` and `error_rate` trace features are set to zero by policy; this
means telemetry is unavailable for those services, not that measured traffic
or errors are zero. Resource metrics for these pods are still collected.
