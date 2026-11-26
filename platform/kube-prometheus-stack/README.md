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
| Grafana | `https://grafana.int.0xc0.cc`, WARP only; the chart's default dashboards and ours (Dashboards, below); Prometheus, Loki and Tempo (`platform/loki`, `platform/tempo`) as data sources, Tempo and Loki linked by trace ID; no persistence. The `grafana` container requests 384Mi and is limited to 1Gi |
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
A second group, `cloudflare`, in the same folder, watches the edge's 5xx share
and the tunnel connectors ([Cloudflare](#cloudflare)).

To add one, copy a rule in `alerting-rules.yaml` with a new `uid`. The file goes
through Helm's `tpl`, so write no pair of braces in it. Grafana stores a
query's model and the annotations as written, so write a `$` once there; in a
title, a label and a group's name or folder it expands `$NAME` from its
environment, so a literal `$` is `$$` there.

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
| `platform/grafana/cloudflare` | `api_token` | Grafana, through the Secret `cloudflare-api` and the variable `CLOUDFLARE_API_TOKEN`, which the data source `cloudflare` expands (Cloudflare, below) |

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
| Estate | `home` | The estate on one screen: a health score, the KPI strip, the map of hosts, pools and nodes, traffic from entrypoint to service, golden signals per public site, the tools behind WARP, memory by pool, the log pulse and the latest errors, pool capacity, and changes |
| Apps | `apps` | One application, chosen with `app`: header, availability against its SLO, web vitals, traffic, top pages and devices, two releases compared, latency, errors and pods |
| Matchmark | `matchmark` | Its API, the factory worker and Temporal, and its tasks while they run |
| Web vitals | `applications-rum` | Real-user web vitals across the sites, from Faro |
| Traces / Overall, Traces / By service | `traces-overall`, `traces-by-service` | Spans, latency and the service map, for every service or one |
| Argo CD, Traefik, Vault, Longhorn, CrowdSec, Kubernetes / Events | `argocd`, `traefik`, `vault`, `longhorn`, `crowdsec`, `kubernetes-events` | The platform, each trimmed to what this estate uses; the `Platform` menu lists them by the tag `platform` |
| Right-sizing | `rightsizing` | What every workload asks for against what it used over the range (7 days by default): idle reservation, the pools, asked against used per workload, the memory reservation by namespace, and the containers closest to trouble with the change to make. In `Platform`, and linked from Estate's Pool capacity |

#### Right-sizing

A workload is a Deployment, StatefulSet or DaemonSet; each container counts
at its largest across the workload's pods. Jobs, CronJobs and static pods
(RKE2's control plane) are left out of every block but Pools, which counts
every running and pending pod. Used is the 95th percentile of the 5-minute
CPU rate, and the maximum of the memory working set, over the range.

| State | When |
|---|---|
| idle | used under 30 % of the request |
| fits | 30 to 100 % |
| short | over 100 % of the request |
| near limit | a container's memory maximum over 80 % of its limit; wins over the others |
| unset | no request |

The suggested change: request at p95 × 1.2, memory limit at the maximum ×
1.5, rounded up to 10m and 16 MiB. The `Based on` card says how much of the
range Prometheus holds, so a short history is not read as a full week, and
until the history fills the range, each chart of At the edge starts at its
first sample.

Names drop the namespace beside them and the chart release they repeat:
`rke2-coredns-rke2-coredns-autoscaler` in `kube-system` reads
`coredns-autoscaler`. The tooltip gives the full name.

The memory treemap folds what is too small to read: inside a namespace, the
workloads under 8 % of its memory become one "N others" tile, and the
namespaces under 2 % of the total one "other namespaces" group. A folded
tile's state is its members' together, unless one is near its limit. The
panel lays the treemap out itself, on a `custom` series, rather than with
ECharts' `treemap`, which draws its tiles opaque and gives a label no measure
of its tile: a name shows whole, broken only after a hyphen, or not at all.

Every query runs once, in the strip of cards; the other blocks read its
frames through the `-- Dashboard --` data source, and each query carries a
`q` label that names it for the panels' code.

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
- **Stats.** Each board opens on one strip of cards, 3 rows tall, drawn by
  Business Charts: the label, the value in Roboto Mono with its unit, coloured
  by its threshold, and one small line under it (per hour, "of N", a share),
  short enough for the narrowest card at 1456 px and cut with an ellipsis
  past it. No change across the range. A thin line only under a continuous
  signal (requests), never an area, never under a ratio.
- **Low traffic.** What the estate serves is a few requests an hour, so a
  count is a total over the range (`increase(…[$__range])`) and a chart
  counts per hour, never per second. A percentile needs enough samples in
  its window (20 for a card, 10 for a point) or reads `—`. Rows and series
  without data are left out, and a gap stays a gap (`spanNulls` off, lines
  linear or stepped, never smoothed).
- **Names.** A Traefik backend is a short key server side, its Service as
  `<namespace>-<service>` or `rum-<site>` for a site's Faro route, and its
  readable name comes from one list (`DISPLAY`): value mappings on a table,
  rename transformations on a chart, a lookup in Business Charts' code. A
  Tempo value comes back quoted: a rename takes the quotes off.
- **Public and WARP apart.** Estate's cards count `websecure` alone; what
  reaches the `internal` entrypoint has its own block.
- **Layout.** A KPI strip first, then the blocks that matter most, above the
  fold at 1512×800; sections are open rows, none collapsed.
- **Navigation.** The board links across the top, in this order on every
  board: Estate, Apps, Matchmark, Web vitals, Traces, Platform. A row of
  Golden signals opens the App board with its app.
- **Annotations**, on every board, off by default with their toggles at the
  top: `Argo CD syncs`, one point per sync, an increase of
  `argocd_app_sync_total`; `Releases`, one point where a new image tag of
  ours first appears in an application's namespace, from
  `kube_pod_container_info`, which is also how a promotion shows. Estate
  lists both in its Changes table. OpenTofu applies leave no metric.

### Sources

| What | Where it comes from |
|---|---|
| Node pools | `kube_node_labels{label_0xc0_cc_pool}`: kube-state-metrics exports the `0xc0.cc/pool` label (`metricLabelsAllowlist` in `values.yaml`) |
| Node CPU and memory | node-exporter, named by `node_uname_info`'s `nodename` |
| Workloads, requests and use | A pod's workload from `kube_pod_owner`, through `kube_replicaset_owner` for a Deployment; `kube_pod_container_resource_requests` and `…_limits`; cAdvisor's `container_cpu_usage_seconds_total`, `container_memory_working_set_bytes` and `container_cpu_cfs_*` (only where a CPU limit is set) |
| OOM kills | A change of `kube_pod_container_status_restarts_total` while `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}` holds |
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
| Business Charts, `volkovlabs-echarts-panel` | Every board's strip of cards, the traffic sankey, the memory sunburst, the web vitals gauges, Apps' release comparison and slowest operations, which group by a label no stock panel can pivot on, and every block of Right-sizing |
| Business Text, `marcusolsson-dynamictext-panel` | The estate's health ring, drawn in CSS: its sanitizer strips `<svg>` |
| Infinity, `yesoreyeram-infinity-datasource` | The data source `cloudflare` (Cloudflare, below). Maintained by Grafana Labs; 4.1.8 supports Grafana 12.2.5 and later |

The estate map is Grafana's Canvas: each node's CPU, memory, pods and Ready
are field-bound values, coloured by threshold, since Canvas draws no bar.

## Cloudflare

Cloudflare's edge analytics, and the tunnel connectors' metrics, workspace#305.

### The data source

`cloudflare` (`values.yaml`, `additionalDataSources`) is the Infinity data
source pointed at Cloudflare's GraphQL API, `https://api.cloudflare.com/client/v4/graphql`,
with `api.cloudflare.com` as its only allowed host and a bearer token from
Vault (`platform/grafana/cloudflare`, key `api_token`; the operator writes it,
as the Grafana section says). The token is read-only: Account Analytics Read
on all accounts, Zone Read and Analytics Read on all zones. It reaches Grafana
as an environment variable, expanded into `secureJsonData` when Grafana reads
its provisioning at start, so it is in no ConfigMap.

It is not an exporter. The lablabs exporter, which this replaced, never
queries a Free zone (its `FREE_TIER` only skips them), and every zone is Free.
Asking the API from Grafana needs no Deployment, no scrape and no metrics
store, and a board reads the dataset it wants. The price is that nothing is
kept in Prometheus: history is what Cloudflare keeps.

### What the zones expose

| Dataset | Window |
|---|---|
| `httpRequestsAdaptiveGroups` (status, country, host, path, cache, …) | 30-day queries, 31 days of history, pages of 10,000 |
| `firewallEventsAdaptive` | 30-day queries, 31 days of history |
| `httpRequests1hGroups` | 3 days |
| `httpRequests1dGroups` | 1 year |

The datasets named Adaptive are sampled. `httpRequestsAdaptiveGroups` needs a
filter on `datetime_geq` and `datetime_lt`, in ISO 8601: Infinity fills them
from the dashboard's range with `${__timeFrom:date:iso}` and
`${__timeTo:date:iso}`. Queries go to `viewer.zones(filter: {zoneTag: …})`.

The zones' tags, which are not secret:

| Zone | Tag |
|---|---|
| 0xc0.cc | `068d993d1c3f23e994211457c96476f8` |
| artistlabco.com | `bf3d282f1920e41701c27cc5be3da43e` |
| asrtistlabco.com | `60a42fad95616837c4f586ccd932e6ba` |
| matchmark.dev | `6fd27750f9a3be154e18dba1774ddf00` |
| offby1.cc | `2e5c4ce565da7cd5ba3e9d3588fe576c` |
| sergioaten.cloud | `35b6a78fcef59563a54825ff503f0a27` |

### An example query

Requests by status for one zone over the dashboard's range. In a panel: data
source `cloudflare`, type GraphQL, method POST, parser Backend (JSONata),
format Table, root selector `data.viewer.zones.httpRequestsAdaptiveGroups`,
columns `dimensions.edgeResponseStatus` (number) and `count` (number):

```graphql
query {
  viewer {
    zones(filter: {zoneTag: "2e5c4ce565da7cd5ba3e9d3588fe576c"}) {
      httpRequestsAdaptiveGroups(
        limit: 100
        filter: {datetime_geq: "${__timeFrom:date:iso}", datetime_lt: "${__timeTo:date:iso}"}
      ) {
        count
        dimensions { edgeResponseStatus }
      }
    }
  }
}
```

### Quota

Cloudflare allows 300 GraphQL queries per 5 minutes per token, and every panel
query and every rule evaluation counts. A board that uses it refreshes no
faster than every 5 minutes.

### The edge 5xx alert

`edge-5xx-above-5` is a Grafana-managed rule on a backend query to this data
source: one GraphQL query for every zone (`zoneTag_in`) that returns the total
and the 5xx counts over the last 10 minutes, and a JSONata root selector that
keeps the zones with at least 50 requests and returns each one's 5xx share. A
threshold above 5% fires per zone. A zone without enough traffic returns no
row, which is not an alert (`noDataState: OK`). The selector wraps the mapping
in `[ … ]`: an empty JSONata result is an error for Infinity, while an empty
array is an empty frame, which is NoData and so OK.

### Tunnel connectors

Prometheus scrapes the four cloudflared connectors as the job `cloudflared`
(`ScrapeConfig` in `scrape-configs.yaml`), on port 20241, with the labels
`tunnel` (`public` or `admin`) and `host`:

| Tunnel | Hosts |
|---|---|
| `public` | `vm-lb-01` `10.10.4.11`, `vm-lb-02` `10.10.4.12` |
| `admin` | `vm-access-01` `10.10.0.10`, `vm-access-02` `10.10.0.20` |

`infrastructure` opens `--metrics` on the connectors and the firewall from
`platform`. `cloudflared_tunnel_ha_connections`,
`cloudflared_tunnel_total_requests`, `cloudflared_tunnel_request_errors` and
`cloudflared_tunnel_active_streams` come from there. A connector holds four
connections to the edge: the alert `tunnel-short-of-connections` fires below
four, or when its target is down. `up{job="cloudflared"}` should be 1 for the
four hosts.

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
