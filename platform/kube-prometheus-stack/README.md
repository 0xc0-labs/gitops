# kube-prometheus-stack

Prometheus, Alertmanager and Grafana in the `monitoring` namespace, beside
OpenObserve (workspace#290, phase 1). OpenObserve keeps working as before;
this stack takes nothing from it.

## What runs

| What | How |
|---|---|
| Prometheus | One replica, 30 days of retention (`retentionSize` 16 GB, under the 20 Gi Longhorn volume), a scrape every minute. Selects every ServiceMonitor, PodMonitor, Probe and PrometheusRule in the cluster, not only the chart's. Requests 200m / 1.5 Gi, memory limit 3 Gi |
| OTLP | Prometheus's OTLP receiver is on. The collectors' gateway (`openobserve-collector`) sends it the metrics it receives over OTLP, the applications', as well as to OpenObserve. What the collectors scrape is not forwarded: Prometheus scrapes the same ServiceMonitors itself |
| Alertmanager | One replica, the chart's default rules, a `null` receiver |
| Grafana | `https://grafana.int.0xc0.cc`, WARP only; the chart's default Kubernetes dashboards; Prometheus, Alertmanager, Loki and Tempo (`platform/loki`, `platform/tempo`) as data sources, Tempo and Loki linked by trace ID; no persistence |
| node-exporter, kube-state-metrics | As the chart ships them. node-exporter tolerates the `apps` and `sandbox` pool taints, so every node reports |
| The operator | No admission webhooks |

The CRDs are `prometheus-operator-crds`': the chart's own are off, so each CRD
has one owner. Keep that component on the operator's version.

## Alerts go nowhere

Alertmanager's only receiver is `null`. The rules evaluate and show in its UI
and in Grafana, and reach nobody until a receiver is chosen (workspace#290).

## Grafana

Reached over WARP at `https://grafana.int.0xc0.cc`, on the Gateway's `internal`
listener. Its admin login is the operator's secret, written as [`vault`'s
README, Writing or rotating a secret][vault-write] says:

| Vault | Keys | Read by |
|---|---|---|
| `platform/grafana/admin` | `admin_user`, `admin_password` | Grafana, through the Secret `grafana-admin` |

Until it is written, Grafana's pod waits for the Secret and the rest runs.
Vault Secrets Operator restarts Grafana when the secret changes. Dashboards
and data sources come from git: an edit made in the UI is lost on a restart.

## Control plane

RKE2 exposes the control plane's metrics on the node's address (the
`rke2_server` role in `infrastructure`), within `platform`:

| Job | Port | State |
|---|---|---|
| etcd | 2381, HTTP | scraped |
| kube-controller-manager | 10257, HTTPS, bearer token, no verification | scraped |
| kube-scheduler | 10259, HTTPS, bearer token, no verification | scraped |
| kube-proxy | 10249 | **off**: RKE2 binds it to `127.0.0.1`, and the zone firewall admits no such port between the nodes. Its rules are off too |

node-exporter listens on 9100 of each node. The zone firewall admits `platform`
to `platform` on no 9100 (`infrastructure`'s `transit`): a node-exporter target
on another node than Prometheus's may stay down until that rule exists.

## Pod Security

The namespace is `privileged` because node-exporter needs the host network, PID
and paths. No container in it runs privileged.

## NetworkPolicies

The namespace denies ingress by default. Grafana takes ingress from Traefik
alone; Prometheus from Grafana, the operator and the collectors' gateway
(OTLP); Alertmanager from Grafana, Prometheus and the operator. In the
namespaces that limit ingress and carry a ServiceMonitor (`cert-manager`,
`crowdsec`, `external-dns`, `longhorn-system`, `openobserve`, `traefik`,
`vault`, `vault-secrets-operator`), Prometheus's pods may reach the metrics
port, beside the collectors.

## Storage

Prometheus's volume, 20 Gi on Longhorn, is in PBS's nightly backup of the RKE2
VMs. A block is a handful of files every two hours, so the volume stays small
in files. Grafana and Alertmanager keep nothing.

[vault-write]: https://github.com/0xc0-labs/vault/blob/main/README.md#writing-or-rotating-a-secret
