# tempo

Tempo in the `tempo` namespace, beside OpenObserve (workspace#290, phase 2).
OpenObserve keeps every trace as before; the collectors send a second copy here.

## What runs

| What | How |
|---|---|
| Tempo | Grafana's `tempo` chart, monolithic, one replica. Requests 100m / 768Mi, memory limit 2Gi (the generator holds live traces in memory) |
| Storage | Blocks in the R2 bucket `tempo-blocks`, region `auto`. The Longhorn volume (10 Gi) holds the WAL and the metrics generator's WAL and local blocks (`/var/tempo/generator`) |
| Retention | 14 days (`336h`), enforced by the compactor on the bucket |
| Ingest | OTLP gRPC (`tempo.tempo.svc.cluster.local:4317`) and HTTP (`4318`). The collectors' gateway (`openobserve-collector`) exports its traces there beside OpenObserve |
| Query | Grafana's `Tempo` data source (`platform/kube-prometheus-stack`, port 3200), linked to Loki (trace to logs) and with the service map pointed at Prometheus |
| Metrics generator | On, with the `local-blocks` processor alone: what TraceQL metrics queries (the `Traces` dashboards and `Matchmark`'s time series) read. `service-graphs` and `span-metrics` stay off: the collectors' gateway already feeds Prometheus the `traces_service_graph_*` series (its `servicegraph` connector), and span metrics would duplicate series. Nothing is remote-written. It keeps every span (`filter_server_spans: false`) and flushes its blocks to the bucket, so ranges beyond the last hour answer |

## The operator's secret

Written as [`vault`'s README, Writing or rotating a secret][vault-write] says,
from an R2 API token of the bucket `tempo-blocks` (Object Read & Write):

| Vault | Keys | Read by |
|---|---|---|
| `platform/tempo/r2` | `access_key_id`, `secret_access_key`, `endpoint` (`https://<account id>.r2.cloudflarestorage.com`) | Tempo, through the Secret `r2-vault`, as environment variables the config expands |

Until it is written, the pod waits for the Secret. Vault Secrets Operator
restarts Tempo when the secret changes.

## Checking ingestion

```
kubectl -n tempo get pods
kubectl -n tempo logs statefulset/tempo | grep -i "error\|denied"
```

In Grafana, Explore, `Tempo`, Search: spans of a service appear within a
minute. The collector gateway's
`otelcol_exporter_send_failed_spans_total{exporter="otlp/tempo"}` stays at
zero. A TraceQL metrics query answers (Explore, `Tempo`, `Query type: TraceQL`, `{} | rate()`) a minute or two after the first spans. Blocks appear in `tempo-blocks` once the ingester flushes (every
few minutes under load).

[vault-write]: https://github.com/0xc0-labs/vault/blob/main/README.md#writing-or-rotating-a-secret
