# cloudflare-exporter

Cloudflare's edge analytics as Prometheus metrics, in the `cloudflare-exporter`
namespace (workspace#305). It also documents the tunnels' connector metrics,
which Prometheus scrapes from `platform/kube-prometheus-stack`.

## What runs

| What | How |
|---|---|
| [lablabs/cloudflare-exporter](https://github.com/lablabs/cloudflare-exporter) 0.2.3 | One Deployment, pinned by version and digest, under the service account `cloudflare-exporter`. `FREE_TIER=true`: without it the exporter drops every zone on the Free plan |
| The token | `platform/cloudflare-exporter`, key `api_token`, in Vault, read by Vault Secrets Operator through the separate service account `vault-secrets` (role `cloudflare-exporter`) into the Secret `cloudflare-vault` |
| Scrape | A `ServiceMonitor` every 60 s: each scrape runs GraphQL queries, and Cloudflare allows 300 per 5 minutes |

Backups: no volumes.

## What it exports

`cloudflare_zone_*` for every zone the token reads, the ones in the free
plan's set: requests, bandwidth and threats, by status, country, content type
and colocation, and unique visitors. Some of the exporter's metrics are left
out in free-tier mode (the origin latency quantiles, ASN, load balancer pools
among them). The alerts use `cloudflare_zone_requests_status`.

What a Free zone returns is what the cluster shows. Cloudflare's docs
disagree: the October 2026 changelog says the adaptive datasets keep 31 days
on Free and Pro, while the Worker exporter's README says Free zones get no
GraphQL analytics.

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
should be 1 for the four hosts. The exporter's log says which zones it found.

## NetworkPolicies

The namespace denies ingress and egress by default. Ingress to the exporter
is from Prometheus on 8080. Egress is DNS and 443 outside the private ranges,
for Cloudflare's API.
