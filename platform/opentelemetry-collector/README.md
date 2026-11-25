# opentelemetry-collector

The cluster's collector, in the `opentelemetry` namespace (workspace#290). It
is named after no backend: the applications send OTLP to one address and never
know Loki, Tempo or Prometheus.

## The one entry point

```
otel-gateway.opentelemetry.svc.cluster.local   gRPC 4317, HTTP 4318
```

A sender's namespace needs an egress rule to the pods labelled
`app.kubernetes.io/instance: opentelemetry.otel-gateway`, and an entry in
`otlp-from-senders` (`network-policy.yaml`). `apps/` and `platform/traefik` send here.

The operator names the Service it creates `otel-gateway-collector`; the
`otel-gateway` Service in `gateway.yaml` carries the agreed name.

## What runs

Both are `OpenTelemetryCollector` resources of the operator
(`platform/opentelemetry-operator`), on the contrib distribution, pinned by
digest.

| What | How |
|---|---|
| `otel-agent` | A DaemonSet on every node, the tainted pools included. Reads `/var/log/pods` (`filelog`), parses the severity and the org's JSON log line with its `trace_id` (as the old agent did), adds the Kubernetes attributes and exports to Loki over OTLP |
| `otel-gateway` | One Deployment on the `infra` pool. OTLP in; traces to Tempo, metrics to Prometheus, logs and the Kubernetes events to Loki |

Pipelines:

| Pipeline | From | To |
|---|---|---|
| agent `logs` | `filelog/std` | Loki |
| gateway `traces` | OTLP | Tempo, and the `servicegraph` connector |
| gateway `metrics` | OTLP, `servicegraph` | Prometheus (`/api/v1/otlp`) |
| gateway `logs` | OTLP | Loki |
| gateway `logs/k8s_events` | `k8sobjects/events` | Loki |
| gateway `logs/faro` | `faro` | Loki |
| gateway `traces/faro` | `faro` | Tempo |

The agent keeps how far it read each file in `/var/lib/otelcol` on the node
(`file_storage`), and its exporter queue too. A restart goes on from there;
without it, the agent read every file again and Loki rejected the old lines as
"too far behind". With no offset (a node's first run) it starts at the end.

## Faro

The sites' browsers send RUM with Grafana's Faro SDK, as JSON, to the contrib
distribution's `faro` receiver (alpha; the OTLP of a browser is not used). The
receiver listens on `8886` of `otel-gateway`, on any path, and takes `POST`
with `Content-Type: application/json` alone; its body is limited to 1 MiB
(`max_request_body_size`). It has no token: the rate limit and CrowdSec are
the protection.

```
browser -> POST https://<site>/rum/faro -> Traefik (CrowdSec, rate limit) -> otel-gateway:8886
```

Each site's routes carry the rule (`apps/*/*/httproute.yaml`): `POST` on the
prefix `/rum/faro`, rewritten to `/`, with that namespace's `rum-rate-limit`
middleware, to `otel-gateway` in this namespace. `reference-grant.yaml` admits
the sites' namespaces to it, and `faro-from-traefik` admits Traefik to the port.
The CORS and the preflight never come into play: the intake is same-origin.
POST responses are not cached by Cloudflare, so nothing is set for it.

The `logs/faro` and `traces/faro` pipelines have no `k8sattributes`: the
connection is Traefik's, and the processor would stamp the browser's records
with Traefik's pod. A record's `service.name` is the app name the site's SDK
sets, which Loki indexes as `service_name`; its `kind` (`log`, `exception`,
`measurement`, `event`) is structured metadata, and the rest of Faro's meta
(page, session, browser) is in the logfmt body. The dashboard `Applications /
RUM` (`platform/kube-prometheus-stack`) reads them.

Faro's intake can be written to by anyone, and `service.name` comes from the
payload: the rate limit bounds how fast a stream label could be invented,
not which.

## No scraping here

Prometheus (`platform/kube-prometheus-stack`) scrapes the ServiceMonitors,
and its node-exporter, kube-state-metrics and the kubelet cover what
`hostmetrics`, `kubeletstats` and the target allocator did. A collector that
scraped too would duplicate every series.

The metrics the applications push reach Prometheus with the resource
attributes `service.name`, `service.namespace`, `k8s.namespace.name`,
`k8s.pod.name` and `deployment.environment` as labels
(`prometheus.prometheusSpec.otlp.promoteResourceAttributes`). The SDKs send
cumulative temporality, which Prometheus expects: an application that sends
delta needs Prometheus's delta-to-cumulative conversion on.

## Checking that it exports

The collectors' own metrics are scraped (`servicemonitor.yaml`). In Grafana,
Explore, `Prometheus`:

```
sum by (exporter, pod) (rate(otelcol_exporter_send_failed_log_records_total[5m]))
sum by (exporter, pod) (rate(otelcol_exporter_send_failed_spans_total[5m]))
sum by (exporter, pod) (rate(otelcol_exporter_send_failed_metric_points_total[5m]))
```

Any value above zero names the exporter (`otlphttp/loki`, `otlp/tempo`,
`otlphttp/prometheus`) that fails; `kubectl -n opentelemetry logs
deployment/otel-gateway-collector` says why. `otelcol_exporter_queue_size`
against `otelcol_exporter_queue_capacity` shows a backend that is slow.

## Network

The namespace denies ingress and egress by default. The collectors reach DNS,
the Kubernetes API (the three RKE2 servers, since Canal translates a Service
address first), Loki on 3100 and, the gateway only, Tempo on 4317 and
Prometheus on 9090. Traefik reaches the gateway's Faro port, 8886. Loki, Tempo and Prometheus admit `opentelemetry`.

## Matchmark's usage record

The `logs/matchmark_usage` pipeline takes the log records of the OTLP receiver,
keeps those with `gen_ai.usage.input_tokens` or `gen_ai.usage.output_tokens`
and posts them to Matchmark's API (`http://api.matchmark.svc.cluster.local/otlp`),
which writes a row each into `usage_ledger` (`apps/matchmark/README.md`, The
usage record). The logs pipeline to Loki takes the same records too, so each
model call's access log is also in Loki.
