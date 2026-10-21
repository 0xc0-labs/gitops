# artistlabco-com

The artist's site, the Node server of 0xc0-labs/artistlabco.com, a front of
Payload (operator decision, 2026-10-13; workspace#168), and the site's
Strapi beside it (The CMS, below), which replaces Payload (workspace#202). It
is a BFF: the browser reaches it alone, and it calls its environment's Payload
at `http://payload.<namespace>.svc` ([`../payload/README.md`](../payload/README.md),
Fronts) and its Strapi at `CMS_URL`, until the front stops reading Payload
(0xc0-labs/artistlabco.com#82). Two environments, each an Argo CD application
of its own:

| | prod | staging |
|---|---|---|
| Application and namespace | `artistlabco-com` | `artistlabco-com-staging` |
| Address | `https://artistlabco.com`, public; `www.artistlabco.com` redirects there | `https://artistlabco-com-staging.int.0xc0.cc`, over WARP, and `https://staging.artistlabco.com`, public behind Cloudflare Access |
| Payload | `payload` | `payload-staging` |
| CMS admin | `https://cms.artistlabco.com`, public behind Cloudflare Access | `https://cms-staging.artistlabco.com`, public behind Cloudflare Access |
| Replicas | front 2, with a PodDisruptionBudget; CMS 1 | front 1; CMS 1 |
| Vault | `apps/artistlabco-com/*` | `apps/artistlabco-com-staging/*` |

Staging's public name sits behind a Cloudflare Access application, created in
`infrastructure`: a visitor gets in with an email in a group the operator
manages in Cloudflare's dashboard (how:
[`modules/access-app`](https://github.com/0xc0-labs/infrastructure/tree/main/modules/access-app)).
Its route also sends `X-Robots-Tag: noindex, nofollow`, in case Access is ever
misconfigured.

`base/` holds what both share; `prod/` and `staging/` add the namespace, the
routes, the Payload and CMS URLs and their egress, and the pins. No volumes:
the front holds no data, and the CMS keeps its in the shared PostgreSQL
(nightly dump by `platform/postgres`, `postgres-dumps/<namespace>`) and R2.

## The CMS

Strapi 5, from `cms/` of 0xc0-labs/artistlabco.com: one Deployment and Service
`artistlabco-com-cms` in each namespace, service account `cms`, with its own
`app.kubernetes.io/name` so the front's Service, PDB and policies never select
it (`base/cms.yaml`).

- **One replica, `Recreate`.** Strapi syncs its content-type schema to the
  database at start, and two pods doing it at once race on the same tables, as
  a `RollingUpdate`'s surge would. The cost is a short gap on each rollout.
  Prod's PDB is `maxUnavailable: 1`, since a `minAvailable` of 1 on one pod
  would block every node drain.
- **Probes.** `/healthz` for startup and liveness, which never touches the
  database; `/readyz` for readiness, which does.
- **Filesystem.** Read-only root, with `.tmp`, `.strapi` and `public/uploads`
  under `/app` as `emptyDir`. Media goes to R2, so nothing persists.
- **Resources.** Requests and limits are an estimate from Payload's, not yet
  measured on staging.
- **Database.** `DATABASE_SSL=false`: the shared PostgreSQL serves no TLS, and
  Payload connects the same way; the traffic stays inside the cluster, under a
  NetworkPolicy that admits the CMS's pods alone. The role and the database are
  named after the namespace (`DATABASE_NAME`, `DATABASE_USERNAME`).
- **Proxies.** `PROXY_HOPS=2`: Cloudflare and Traefik, in front of the admin's
  public route.
- **Admin.** `cms.artistlabco.com` and `cms-staging.artistlabco.com`, public
  and behind Cloudflare Access (`infrastructure`). Without Access applied the
  admin would be open: do not sync the route before it. The front
  reads the API over the Service at `CMS_URL`, with `CMS_API_TOKEN`.
- **Vault identity.** The namespaces carry `vault.0xc0.cc/cms`, which admits
  the service account `cms` to the Kubernetes auth role `cms`
  ([`vault`](https://github.com/0xc0-labs/vault)): its projected token at
  `/var/run/secrets/vault/token` is how Strapi writes an integration's key to
  `apps/<namespace>/integrations/<name>`. It never reads one back to the admin.
- **The way out.** Default deny, and then: PostgreSQL, Vault, the collector,
  DNS, HTTPS to any public address (Stripe, Bunny, Discord, Resend and R2 have
  no range to name: the same accepted risk as Payload's), and, in prod,
  `mautic-artistlabco-com`. The way in: the gateway, and the front's pods.

## Before the first sync: the secrets

The operator writes them as [`vault`'s README, Writing or rotating a
secret][vault-write] says, each environment at its own path:

| Path | Key | Value |
|---|---|---|
| `apps/<namespace>/payload` | `api_key` | Payload's API key for this front, made in its admin: read as `PAYLOAD_API_KEY` |
| `apps/<namespace>/preview` | `secret` | generated: `openssl rand -base64 32`, the same value as Payload's `apps/payload(-staging)/preview`: read as `PREVIEW_SECRET` |
| `apps/<namespace>/database` | `password` | the CMS's PostgreSQL role, `DATABASE_PASSWORD`; `platform/postgres` reads it too, to create the role and the database |
| `apps/<namespace>/cms` | `APP_KEYS`, `ADMIN_JWT_SECRET`, `API_TOKEN_SALT`, `TRANSFER_TOKEN_SALT`, `JWT_SECRET`, `ENCRYPTION_KEY`, `SUPERADMIN_EMAIL`, `SUPERADMIN_PASSWORD` | Strapi's secrets, each key named as its variable, and the first super admin |
| `apps/<namespace>/cms-client` | `api_token` | the front's read-only Strapi API token: `CMS_API_TOKEN` for the front, `FRONT_API_TOKEN` for Strapi. Without it neither starts |

OpenObserve's RUM token is `apps/shared/openobserve-rum`, and the image's pull
token `apps/shared/ghcr-pull`, since the package is private: both already
there. Vault needs no change: its `apps` role admits any namespace labelled
`vault.0xc0.cc/apps`.

## Upgrades

The pin is each environment's `kustomization.yaml`, written by the promoter
in 0xc0-labs/artistlabco.com: staging follows every release candidate
(`vX.Y.Z-rc.N`), and a release, run by hand on what staging runs, changes
prod's, whose merge is prod's approval. Both start as `v0.0.0` and an
all-zero digest: until the first rc and the first release, their syncs fail on
the image.

```yaml
images:
  - name: ghcr.io/0xc0-labs/artistlabco.com
    newTag: v0.1.0
    digest: sha256:<64 hex>
configMapGenerator:
  - name: release
    literals:
      - APP_VERSION=v0.1.0
      - APP_ENVIRONMENT=prod
```

The CMS's image is pinned beside the front's, with the same `newTag` and
`APP_VERSION`: one version covers both, and the promoter rewrites both pins
(`.github`'s README, "A second image"). The digest decides what runs.

Every signal carries the version and the environment, from the `release`
ConfigMap: `OTEL_RESOURCE_ATTRIBUTES`. To roll back, revert the PR: the
previous pin comes back with its digest.

## Headers

Traefik sets the baseline on every entrypoint
(`platform/traefik/security-headers.yaml`) and each overlay's route adds the
app's own (`security-headers` in `httproute.yaml`: no framing,
Permissions-Policy, COOP/CORP). The app sets its Content-Security-Policy.

## The way out

Default deny, and then: traces to the collector, DNS, its Payload's pods on
3000, the CMS's pods on 1337, and HTTPS to Cloudflare's ranges for `media.0xc0.cc`, which `next/image`
fetches (`base/network-policy.yaml`, `egress-media`: the ranges are
Cloudflare's, so the platform allows no narrower rule). Each route also
carries the browsers' RUM to OpenObserve.

[vault-write]: https://github.com/0xc0-labs/vault/blob/main/README.md#writing-or-rotating-a-secret
