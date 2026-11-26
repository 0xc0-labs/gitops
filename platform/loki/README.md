# loki

Loki in the `loki` namespace: the cluster's logs (workspace#290), read in
Grafana.

## What runs

| What | How |
|---|---|
| Loki | Grafana's chart in its single-binary mode, one replica, no gateway, caches, canary or self-monitoring. Requests 250m / 1Gi, memory limit 2Gi |
| Storage | Chunks and the `tsdb` index (schema `v13`) in the R2 bucket `loki-chunks`, region `auto`. The Longhorn volume (10 Gi) holds the WAL and the compactor's scratch space only |
| Retention | 30 days (`720h`), enforced by the compactor, with its delete requests on the same bucket |
| Ingest | OTLP HTTP at `http://loki.loki.svc.cluster.local:3100/otlp`. The collectors' agent (`opentelemetry-collector`), which reads every container's log, exports there |
| Labels | Three: `service_name`, `k8s_namespace_name`, `k8s_pod_name`. Every other attribute is structured metadata, searchable but not a stream |
| Query | Grafana's `Loki` data source (`platform/kube-prometheus-stack`), linked to Tempo by `trace_id` |

Why R2 and not a Longhorn volume: Loki writes a file per chunk, and a node
that relabels a volume's files at each start gives up on millions of them
(a pod looped on 2026-11-25).

## The operator's secret

Written as [`vault`'s README, Writing or rotating a secret][vault-write] says,
from an R2 API token of the bucket `loki-chunks` (Object Read & Write):

| Vault | Keys | Read by |
|---|---|---|
| `platform/loki/r2` | `access_key_id`, `secret_access_key`, `endpoint` (`https://<account id>.r2.cloudflarestorage.com`) | Loki, through the Secret `r2-vault`, as environment variables the config expands |

Until it is written, the pod waits for the Secret. Vault Secrets Operator
restarts Loki when the secret changes.

## Checking ingestion

```
kubectl -n loki get pods
kubectl -n loki logs statefulset/loki | grep -i "error\|denied"
```

In Grafana, Explore, `Loki`: `{k8s_namespace_name="argocd"}`. The collector
agent's `otelcol_exporter_send_failed_log_records_total{exporter="otlphttp/loki"}`
stays at zero. Objects appear in `loki-chunks` within minutes (chunks flush
after the chunk idle period).

[vault-write]: https://github.com/0xc0-labs/vault/blob/main/README.md#writing-or-rotating-a-secret
