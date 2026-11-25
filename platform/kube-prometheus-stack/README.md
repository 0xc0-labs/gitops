# kube-prometheus-stack

Prometheus and Grafana in the `monitoring` namespace. Grafana is the one view
of the cluster: metrics from Prometheus, logs from Loki (`platform/loki`),
traces from Tempo (`platform/tempo`), and the alerts (workspace#290).

## What runs

| What | How |
|---|---|
| Prometheus | One replica, 30 days of retention (`retentionSize` 16 GB, under the 20 Gi Longhorn volume), a scrape every minute. Selects every ServiceMonitor, PodMonitor, Probe and PrometheusRule in the cluster, not only the chart's. Requests 200m / 1.5 Gi, memory limit 3 Gi |
| OTLP | Prometheus's OTLP receiver is on. The collectors' gateway (`opentelemetry-collector`) sends it the metrics it receives over OTLP, the applications'. The collectors scrape nothing: Prometheus scrapes the ServiceMonitors itself |
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
- The collectors' metrics reach Prometheus through a `ServiceMonitor` in
  `opentelemetry` (`platform/opentelemetry-collector`).
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

Every dashboard is provisioned from git and read-only in the UI. The chart's
defaults cover the Kubernetes ones; the rest are JSON files in
`dashboards/<folder>/`, each turned by `kustomization.yaml` into a ConfigMap
labelled `grafana_dashboard: "1"` with a `grafana_folder` annotation, which
Grafana's sidecar loads into that folder: `Kubernetes`, `Platform`,
`Applications`, `Traces`. `Estate` is the exception: Grafana's home dashboard,
mounted as a file (`values.yaml`). The component's `Prune=false` reaches the
ConfigMaps: deleting a dashboard's entry leaves its ConfigMap to delete by
hand.

To add one, put its JSON in `dashboards/<folder>/<name>.json` with a stable
`uid`, `"id": null` and its data sources as `{"type": "prometheus", "uid":
"prometheus"}` (`loki`, `tempo` likewise), then add a `configMapGenerator`
entry beside the others with its folder. One ConfigMap per dashboard: Argo CD's
last-applied annotation caps one near 256 KiB.

### The boards

| Board | `uid` | For |
|---|---|---|
| Estate | `home` | The estate on one screen: a health score, the KPI strip, the map of hosts, pools and nodes, traffic from entrypoint to service, golden signals per service, memory by pool, the log pulse, pool capacity, and changes |
| Apps | `apps` | One application, chosen with `app`: header, availability against its SLO, web vitals, traffic, top pages and devices, two releases compared, latency, errors and pods |
| Matchmark | `matchmark` | Its API, the factory worker and Temporal, and its tasks while they run |
| Web vitals | `applications-rum` | Real-user web vitals across the sites, from Faro |
| Traces / Overall, Traces / By service | `traces-overall`, `traces-by-service` | Spans, latency and the service map, for every service or one |
| Argo CD, Traefik, Vault, Longhorn, CrowdSec, Kubernetes / Events | `argocd`, `traefik`, `vault`, `longhorn`, `crowdsec`, `kubernetes-events` | The platform, each trimmed to what this estate uses; the `Platform` menu lists them by the tag `platform` |

### Design

One look across every board, a control room (operator, 2026-11-25).

- **Palette.** Grafana's dark ground, every panel transparent over it. Where a
  block draws its own surface: `#10151C`, inner `#0D1218` / `#131A23`, lines
  `#1F2733`. Text `#E7ECF3`, secondary `#C9D2DE`, labels `#8D99AB`.
- **Status.** ok `#34D399`, warning `#FBBF24`, critical `#FB7185`, and always
  with a word or a sign beside the colour (`● ready`, `▲ higher`).
- **Series.** blue `#60A5FA` for traffic, requests and CPU; amber `#F59E0B`
  for latency and memory; violet `#C4B5FD` and cyan `#7DD3FC` for the rest.
  Pools: infra cyan, apps amber, sandbox violet.
- **Type.** Grafana's Inter. The Business Text blocks set numbers, names and
  versions in Roboto Mono, labels small, uppercase and letter-spaced, and
  headings in Space Grotesk where the browser has it. Grafana's own panels
  take no font setting.
- **Stats.** The value coloured by its threshold or its series, never the
  panel; the change across the range under it (`showPercentChange`, inverted
  where lower is better); a sparkline along the bottom.
- **Layout.** A KPI strip first, then the blocks that matter most; sections
  are open rows, none collapsed.
- **Navigation.** The board links across the top, in this order on every
  board: Estate, Apps, Matchmark, Web vitals, Traces, Platform. A row of
  Golden signals opens the App board with its app.
- **Annotations**, on every time series of every board: `Argo CD syncs`, an
  increase of `argocd_app_sync_total`; `Releases`, a new image tag of ours in
  an application's namespace, from `kube_pod_container_info`, which is also
  how a promotion shows. OpenTofu applies leave no metric.

### Sources

| What | Where it comes from |
|---|---|
| Node pools | `kube_node_labels{label_0xc0_cc_pool}`: kube-state-metrics exports the `0xc0.cc/pool` label (`metricLabelsAllowlist` in `values.yaml`) |
| Node CPU and memory | node-exporter, named by `node_uname_info`'s `nodename` |
| Traffic, golden signals, availability | Traefik's `traefik_entrypoint_*` and `traefik_service_*`. A service is the part of `exported_service` after `-svc-`; an application is the namespace after `httproute-` |
| Service maps, calls between services | The collectors' `servicegraph` connector (`traces_service_graph_*`). Peers known only by a pod IP, or `unknown`, are Traefik's backends that send no spans: the panels leave them out |
| Versions | The image tag in `kube_pod_container_info`'s `image_spec`, `service.version` on spans, `app_version` on Faro's lines |
| Web vitals, pages, devices, browser exceptions | Faro's lines in Loki: `kind` (structured metadata) tells them from the container logs; one vital per `type=web-vitals` line, `value_<vital>`; the orchestrator's probes (`app_environment=probe`) left out |
| Log levels | Loki's `detected_level`, set to lower case in the query |
| Vault tokens | `vault_token_count_by_auth` and `…_by_ttl`, periodic gauges, read through `last_over_time(…[15m])`. No audit device is enabled, so there is no audit panel |
| Longhorn | Each volume metric comes from several managers and its `node` is the reporter's: read once per volume (`max by (volume)`). Replicas per node from `longhorn_replica_info` |
| Matchmark's tasks | `matchmark.task` and `matchmark.attempt` spans, which exist only while tasks run: the panels say "no tasks in this range" otherwise, and `app` and `project` are free-text filters |

### Plugins

`grafana.plugins` in `values.yaml`, pinned, downloaded at each start:

| Plugin | For |
|---|---|
| Business Charts, `volkovlabs-echarts-panel` | The traffic sankey and the memory sunburst |
| Business Text, `marcusolsson-dynamictext-panel` | The estate's health ring |

The estate map is Grafana's Canvas: each node's CPU, memory, pods and Ready
are field-bound values, coloured by threshold, since Canvas draws no bar.

## Control plane

RKE2 exposes the control plane's metrics on the node's address (the
`rke2_server` role in `infrastructure`), within `platform`:

| Job | Port | State |
|---|---|---|
| etcd | 2381, HTTP | scraped |
| kube-controller-manager | 10257, HTTPS, bearer token, no verification | scraped |
| kube-scheduler | 10259, HTTPS, bearer token, no verification | scraped |
| kube-proxy | 10249 | **off**: RKE2 binds it to `127.0.0.1`, and the zone firewall admits no such port between the nodes. Its rules are off too |

node-exporter listens on 9100 of each node, and every node's target is up
(infrastructure#325).

## Pod Security

The namespace is `privileged` because node-exporter needs the host network, PID
and paths. No container in it runs privileged.

## NetworkPolicies

The namespace denies ingress by default. Grafana takes ingress from Traefik
alone; Prometheus from Grafana, the operator and the collectors' gateway
(OTLP). In the
namespaces that limit ingress and carry a ServiceMonitor (`cert-manager`,
`crowdsec`, `external-dns`, `longhorn-system`, `traefik`,
`vault`, `vault-secrets-operator`), Prometheus's pods may reach the metrics
port.

## Storage

Prometheus's volume, 20 Gi on Longhorn, is in PBS's nightly backup of the RKE2
VMs. A block is a handful of files every two hours, so the volume stays small
in files. Grafana keeps nothing.

[vault-write]: https://github.com/0xc0-labs/vault/blob/main/README.md#writing-or-rotating-a-secret
