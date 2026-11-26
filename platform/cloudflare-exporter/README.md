# cloudflare-exporter

Cloudflare's edge analytics as Prometheus metrics, in the `cloudflare-exporter`
namespace (workspace#305). It also documents the tunnels' connector metrics,
which Prometheus scrapes from `platform/kube-prometheus-stack`.

## What runs

| What | How |
|---|---|
| [lablabs/cloudflare-exporter](https://github.com/lablabs/cloudflare-exporter) 0.2.3 | One Deployment, pinned by version and digest, under the service account `cloudflare-exporter`. `FREE_TIER=true`, `LOG_LEVEL=debug` (settings below) |
| The token | `platform/cloudflare-exporter`, key `api_token`, in Vault, read by Vault Secrets Operator through the separate service account `vault-secrets` (role `cloudflare-exporter`) into the Secret `cloudflare-vault` |
| Scrape | A `ServiceMonitor` every 60 s: each scrape runs GraphQL queries, and Cloudflare allows 300 per 5 minutes |

Backups: no volumes.

## Settings

What the exporter reads in 0.2.3 (environment variables or flags), and what
this deployment sets:

| Variable | Default | Set here | Notes |
|---|---|---|---|
| `CF_API_TOKEN` | none | from Vault | Needs Zone › Analytics › Read for zone metrics; Account › Account Analytics › Read and Account › Account Settings › Read for the account's Workers, logpush and R2; Zone › Firewall Services › Read and Account › Account Rulesets › Read for rule names. Listing zones and accounts needs Zone › Zone › Read. A failure to list zones or accounts ends the process |
| `CF_API_EMAIL`, `CF_API_KEY` | none | no | A Global API Key instead of the token; not used |
| `CF_ZONES` | all zones the token reads | no | Comma-separated zone IDs. The docs do not require it with a scoped token: the exporter lists every zone the token can read and filters by it |
| `CF_EXCLUDE_ZONES` | none | no | Comma-separated zone IDs to skip |
| `FREE_TIER` | `false` | `true` | See below |
| `SCRAPE_DELAY` | `300` s | default | The window of every GraphQL query ends this many seconds in the past, so a series first shows up about 5 minutes after start |
| `CF_BATCH_SIZE` | `10` | default | Zones per GraphQL request, 1 to 10 |
| `METRICS_DENYLIST` | none | no | Comma-separated metrics to leave out |
| `LOG_LEVEL` | `info` | `debug` | logrus levels. At `debug` it logs the token's status and the GraphQL requests of the R2 query; the zone queries log errors only |
| `LISTEN` | `:8080` | `:8080` | |
| `METRICS_PATH` | `/metrics` | default | |
| `ENABLE_PPROF` | `false` | default | |

The exporter refreshes every 60 s, fixed: there is no setting for the
interval, and `SCRAPE_DELAY` is not it.

## What it exports

With `FREE_TIER=true`, `fetchZoneAnalytics`, the colocation, load balancer
and zone logpush queries return before asking Cloudflare anything, so no
`cloudflare_zone_*` series exists, whatever the plan. With `false`, those
queries run, but only for zones whose plan is not Free
(`filterNonFreePlanZones`). In 0.2.3, then, no setting makes a Free zone
report. What `FREE_TIER=true` still leaves is account-level: the R2 storage
and operations, and Workers.

If every zone is Free, the exporter will not do what this component was
for: the decision is a different exporter, or Cloudflare's own Worker
exporter (below), with the operator's say. Until then the alerts on
`cloudflare_zone_requests_status` have no data.

Cloudflare's docs disagree on what a Free zone returns: the October 2026
changelog says the adaptive datasets keep 31 days on Free and Pro, while the
Worker exporter's README says Free zones get no GraphQL analytics.

Why not Cloudflare's own Worker exporter: it is a Worker in the account, one
more thing to deploy and secure there, and the cluster already has the
pipeline to scrape a Deployment.

## Tunnel connectors

Prometheus scrapes the four cloudflared connectors as the job `cloudflared`
(`ScrapeConfig` in `platform/kube-prometheus-stack/scrape-configs.yaml`), on
port 20241, with the labels `tunnel` (`public` or `admin`) and `host`:

| Tunnel | Hosts |
|---|---|
| `public` | `vm-lb-01` `10.10.4.11`, `vm-lb-02` `10.10.4.12` |
| `admin` | `vm-access-01` `10.10.0.10`, `vm-access-02` `10.10.0.20` |

`infrastructure` opens `--metrics` on the connectors and the firewall from
`platform`. `cloudflared_tunnel_ha_connections`,
`cloudflared_tunnel_total_requests`, `cloudflared_tunnel_request_errors` and
`cloudflared_tunnel_active_streams` come from there. A connector holds four
connections to the edge: the alert fires below four, or when its target is
down.

## Alerts

Two Grafana rules in `platform/kube-prometheus-stack/alerting-rules.yaml`,
with no contact point yet like the others: a zone with more than 5% of its
requests as 5xx at the edge for 10 minutes (at least 50 requests in the
window), and a connector short of connections or down for 5 minutes.

## Checking it

After a sync, in Prometheus: `count by (__name__, zone) ({__name__=~"cloudflare_.+"})`
lists which metrics have samples for each zone, and `up{job="cloudflared"}`
should be 1 for the four hosts. The exporter's debug log shows the token's status and the R2 query; a zone query that fails logs `failed to fetch zone totals`.

## NetworkPolicies

The namespace denies ingress and egress by default. Ingress to the exporter
is from Prometheus on 8080. Egress is DNS and 443 outside the private ranges,
for Cloudflare's API.
