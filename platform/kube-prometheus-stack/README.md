# kube-prometheus-stack

Prometheus and Grafana in the `monitoring` namespace, beside OpenObserve
(workspace#290). OpenObserve keeps working as before;
this stack takes nothing from it.

## What runs

| What | How |
|---|---|
| Prometheus | One replica, 30 days of retention (`retentionSize` 16 GB, under the 20 Gi Longhorn volume), a scrape every minute. Selects every ServiceMonitor, PodMonitor, Probe and PrometheusRule in the cluster, not only the chart's. Requests 200m / 1.5 Gi, memory limit 3 Gi |
| OTLP | Prometheus's OTLP receiver is on. The collectors' gateway (`openobserve-collector`) sends it the metrics it receives over OTLP, the applications', as well as to OpenObserve. What the collectors scrape is not forwarded: Prometheus scrapes the same ServiceMonitors itself |
| Alertmanager | Off: alerting is Grafana's (Alert rules, below) |
| Grafana | `https://grafana.int.0xc0.cc`, WARP only; the chart's default dashboards and ours (Dashboards, below); Prometheus, Loki and Tempo (`platform/loki`, `platform/tempo`) as data sources, Tempo and Loki linked by trace ID; no persistence |
| node-exporter, kube-state-metrics | As the chart ships them. node-exporter tolerates the `apps` and `sandbox` pool taints, so every node reports |
| The operator | No admission webhooks |

The CRDs are `prometheus-operator-crds`': the chart's own are off, so each CRD
has one owner. Keep that component on the operator's version.

## Alert rules

Alerting is Grafana Alerting's, not Alertmanager's (operator, 2026-11-25: «no
se podría usar mejor grafana alerts? me gustan más»): one place for rules,
silences and destinations, with rules over Loki as well as Prometheus. Alertmanager
is off and Prometheus has none to send to.

| Where | What |
|---|---|
| `alerting-rules.yaml` | The Grafana-managed rules, in the folder `Platform`, group `platform`, evaluated every minute. Provisioned from git as a values file of the chart, so an edit made in the UI is lost on a restart |
| The chart's default `PrometheusRule`s | Kept, and evaluated by Prometheus. Grafana lists them read only, as data-source-managed rules: the cluster's state, not something to edit here. Those about Alertmanager, and `Watchdog` and `InfoInhibitor`, are off |

Each Grafana rule is a query (`A`), the last value of each series (`B`) and a
threshold (`C`), with a `summary` and a `runbook_url`. A comment above it says
which default rule or which component's metric its expression comes from. They
cover a volume above 80% of its bytes or inodes, a node `NotReady`, a pod
restarting or OOMKilled, a Vault pod sealed, a certificate expiring in 14 days,
a degraded Longhorn volume, etcd without a leader or a member down, a node's
root filesystem above 85%, a Deployment or StatefulSet short of replicas, a
target down, the collectors failing to export, and, as the example of a log
rule, error lines per namespace in Loki.

To add one, copy a rule in `alerting-rules.yaml` with a new `uid`. The file goes
through Helm's `tpl` and Grafana's own `$` expansion: write no pair of braces
and no dollar sign in it.

- `noDataState` is `OK` on every rule: a series that does not exist is not an
  alert. A metric that never arrives (Vault's, cert-manager's, Longhorn's) is
  caught by `Prometheus target down`, if its target exists.
- `Prometheus target down` leaves out the `node-exporter` job: the zone
  firewall admits no 9100 between the nodes (see Control plane), so its targets
  on other nodes than Prometheus's may stay down. The node's filesystem rule
  sees only the targets that answer.
- The collectors' metrics reach Prometheus through a `PodMonitor` in
  `openobserve-collector`.
- Not covered: the PBS Storage Box's usage. No metric for it reaches
  Prometheus.

### Where alerts go

Nowhere yet. The notification policy is Grafana's default and no contact point
exists, so alerts show in Grafana (`Alerting`, `Alert rules`) and reach nobody
until a contact point is chosen (workspace#290). Contact points carry secrets:
they come from Vault, as a later change.

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

## Dashboards

Every dashboard OpenObserve had exists here (workspace#290), provisioned from
git and read-only in the UI. The chart's defaults cover the Kubernetes ones;
the rest are JSON files in `dashboards/<folder>/`, each turned by
`kustomization.yaml` into a ConfigMap labelled `grafana_dashboard: "1"` with a
`grafana_folder` annotation, which Grafana's sidecar loads into that folder:
`Kubernetes`, `Platform`, `Applications`, `Traces`. The component's `Prune=false`
reaches the ConfigMaps: deleting a dashboard's entry leaves its ConfigMap to
delete by hand.

To add one, put its JSON in `dashboards/<folder>/<name>.json` with a stable
`uid`, `"id": null` and its data sources as `{"type": "prometheus", "uid":
"prometheus"}` (`loki`, `tempo` likewise), then add a `configMapGenerator`
entry beside the others with its folder. One ConfigMap per dashboard: Argo CD's
last-applied annotation caps one near 256 KiB.

| OpenObserve dashboard | In Grafana | Folder |
|---|---|---|
| `kubernetes-clusters-overview` | Default: `Kubernetes / Compute Resources / Cluster` | Kubernetes |
| `kubernetes-nodes` | Default: `Kubernetes / Compute Resources / Nodes Overview` | Kubernetes |
| `kubernetes-nodes-pressure` | Default: `Node Exporter / USE Method / Cluster` and `/ Node` | Kubernetes |
| `kubernetes-node-pods` | Default: `Kubernetes / Compute Resources / Node (Pods)` | Kubernetes |
| `kubernetes-namespaces` | Default: `Kubernetes / Compute Resources / Cluster`, its per-namespace tables | Kubernetes |
| `kubernetes-namespace-pods` | Default: `Kubernetes / Compute Resources / Namespace (Pods)` | Kubernetes |
| `kubernetes-namespace-pod` | Default: `Kubernetes / Compute Resources / Pod` | Kubernetes |
| `kubernetes-api-server` | Default: `Kubernetes / API server` | Kubernetes |
| `etcd` | Default: `etcd` | Kubernetes |
| `host-metrics` | Default: `Node Exporter / Nodes` | Kubernetes |
| `argocd-monitoring` | Imported: `platform/argocd.json` | Platform |
| `longhorn` | Imported: `platform/longhorn.json` | Platform |
| `traefik` | Imported: `platform/traefik.json` | Platform |
| `vault` | Imported: `platform/vault.json` | Platform |
| `crowdsec` | Imported: `platform/crowdsec.json` | Platform |
| `matchmark` | Rebuilt: `applications/matchmark.json`, TraceQL | Applications |
| `apps` | Rebuilt: `applications/apps.json`, PromQL and LogQL | Applications |
| `traces-overall` | Rebuilt: `traces/traces-overall.json`, TraceQL and the service map | Traces |
| `traces-by-service` | Rebuilt: `traces/traces-by-service.json`, the same for one service | Traces |
| `kubernetes-events` | Rebuilt: `kubernetes/kubernetes-events.json`, LogQL | Kubernetes |
| `openobserve` | Dropped: OpenObserve watching itself | |

### Imported ones

Each was edited only to take our data source (`__inputs` removed, `"id": null`,
a fixed `uid`); Traefik's keeps its own data source variable.

| File | Source | Licence |
|---|---|---|
| `argocd.json` | [`argoproj/argo-cd`, `examples/dashboard.json`, tag `v3.6.3`](https://github.com/argoproj/argo-cd/blob/v3.6.3/examples/dashboard.json) | Apache-2.0 |
| `vault.json` | [`adinhodovic/vault-mixin`, `dashboards_out/vault-overview.json`, commit `51529a7`](https://github.com/adinhodovic/vault-mixin/blob/51529a79ea4bef5ae75c62cf8bbeeda621d640c2/dashboards_out/vault-overview.json) | Apache-2.0 |
| `crowdsec.json` | [`crowdsecurity/grafana-dashboards`, `dashboards_v5/Crowdsec Overview.json`, commit `0d34f68`](https://github.com/crowdsecurity/grafana-dashboards/tree/0d34f686930aab1f11562bdf664ffad60c567e39/dashboards_v5) | MIT |
| `traefik.json` | [grafana.com 17347](https://grafana.com/grafana/dashboards/17347), revision 9, published by Traefik Labs | none stated on the page |
| `longhorn.json` | [grafana.com 17626](https://grafana.com/grafana/dashboards/17626), revision 1, the Longhorn example for v1.4.0 | none stated on the page |

### Panels with a caveat

A panel whose data differs from what the old OpenObserve one read says so in
its description:

- **Matchmark and the trace dashboards, their time series and aggregate
  tables** are TraceQL metrics queries, which Tempo answers with its metrics
  generator's `local-blocks` processor (`platform/tempo`). The TraceQL search
  tables of `Failures` and the `Latest traces` tables do not need it.
- **The service maps** read `traces_service_graph_*`, which the collectors'
  gateway makes (its `servicegraph` connector) and sends to Prometheus.
- **`Kubernetes / Events`**: the gateway sends the events to Loki. Its queries
  assume they arrive as JSON lines with `service_name` `unknown_service`.
- **`Apps`' memory and CPU** read cAdvisor in Prometheus, where OpenObserve's
  read the collector's `k8s_pod_*` series.
- **Argo CD's panels** read the four `ServiceMonitor`s in `argocd`, which
  `infrastructure`'s role installs with Argo CD (RKE2's helm-controller) and
  Prometheus scrapes.

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
(OTLP). In the
namespaces that limit ingress and carry a ServiceMonitor (`cert-manager`,
`crowdsec`, `external-dns`, `longhorn-system`, `openobserve`, `openobserve-collector`, `traefik`,
`vault`, `vault-secrets-operator`), Prometheus's pods may reach the metrics
port, beside the collectors.

## Storage

Prometheus's volume, 20 Gi on Longhorn, is in PBS's nightly backup of the RKE2
VMs. A block is a handful of files every two hours, so the volume stays small
in files. Grafana keeps nothing.

[vault-write]: https://github.com/0xc0-labs/vault/blob/main/README.md#writing-or-rotating-a-secret
