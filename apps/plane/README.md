# plane

Plane Community Edition, the project tracker of Matchmark (operator decision,
2026-11-18): the product's tracker, not the org's, whose board stays as it is.
Matchmark's software factory will read and write its work here.

It is at `https://plane.int.0xc0.cc`, **over WARP only and never public**: no
public name, no Cloudflare Access application, and no route carries
`gateway.0xc0.cc/public`. The object store's console and RabbitMQ's management
interface have no route either (`prod/httproute.yaml`).

The chart is `plane-ce` 1.9.1, Plane v1.5.0 (`prod/kustomization.yaml`). Its
database is the shared PostgreSQL's (`platform/postgres`); its Valkey, RabbitMQ
and object store are the chart's own, inside the namespace.

## Why an object store in the cluster

Plane's API signs every upload with a presigned POST, and Cloudflare R2, where
the org's other applications keep their files, signs only GET, HEAD, PUT and
DELETE. So the attachments live in the chart's own store, for Plane alone,
reached through `/uploads/` on the same name: Plane signs an upload for the
host the browser used.

## Before the first sync: the secrets

The operator writes them as [`vault`'s README, Writing or rotating a
secret][vault-write] says, at `apps/plane/`. The passwords that go inside a
URL are generated as hex, so they need no escaping.

| Path | Key | Value |
|---|---|---|
| `plane/database` | `password` | generated: `openssl rand -hex 32`. The role `plane` on the shared PostgreSQL: `platform/postgres` sets it, Plane connects with it |
| `plane/app` | `secret_key` | generated: `openssl rand -hex 32`. Plane's `SECRET_KEY`. **Never rotated**: Plane encrypts its instance configuration with it, and a change makes that unreadable without an error |
| `plane/app` | `live_server_secret_key` | generated: `openssl rand -hex 32`. `LIVE_SERVER_SECRET_KEY`, shared by the API and the live server |
| `plane/rabbitmq` | `password` | generated: `openssl rand -hex 32`. RabbitMQ's user `plane` |
| `plane/storage` | `root_password` | generated: `openssl rand -hex 32`. The object store's root user, `plane`, whose key Plane uses |

Then merge: ArgoCD syncs `postgres`, whose databases Job creates the role and
the database, and then `plane`, with no step by hand. The migrations and the
bucket are Argo CD `Sync` hooks (`prod/kustomization.yaml`): they run again after
every sync, so a new version migrates, and a Job that waits for the database
is retried.

## The first admin

A person's step: after the first sync, open
`https://plane.int.0xc0.cc/god-mode/` over WARP and create the instance's
admin there.

## Backups

Every volume is Longhorn's, in PBS's nightly backup of the RKE2 VMs (03:00):
the object store's 5 Gi, RabbitMQ's 1 Gi and Valkey's 1 Gi. The database is
`platform/postgres`', dumped nightly at 01:30 (`dumps.yaml` there) and
restorable on its own.

## Hardening

The namespace enforces `restricted`, as the org's other applications do. The
chart sets no security context, and Plane's images declare no user and run as
root, so `prod/kustomization.yaml` patches the render: every container,
init containers included, runs `runAsNonRoot` with no privilege escalation,
every capability dropped and the `RuntimeDefault` seccomp profile.

| Workload | Runs as |
|---|---|
| `api`, `worker`, `beat-worker`, `web`, `space`, `admin`, `live`, the migrate and bucket Jobs | uid/gid 1000 (the images ship no user) |
| `minio` | uid/gid 1000, `fsGroup` 1000 (the image runs as any uid) |
| `rabbitmq` | the image's `rabbitmq`, uid 100, gid 101, `fsGroup` 101 |
| `redis` (Valkey) | the image's `valkey`, uid 999, gid 1000, `fsGroup` 1000 |

Every pod has an `emptyDir` on `/tmp`, since the images' own directories belong
to root. Caddy's state (`XDG_*`), `mc`'s and RabbitMQ's `HOME` point there: the
Erlang cookie RabbitMQ wrote on its volume as root cannot be read by its own
user. Its NetworkPolicies deny by default and open only Traefik to the six
routed Services, Plane's pods to each other, DNS and PostgreSQL.

### MinIO's data belongs to uid 1000

The data MinIO wrote before 2026-11-25 belonged to root. Under `restricted`
MinIO runs as uid 1000 and opens its files with `O_NOATIME`, which only the
owner may do, so it failed with "Unable to write to the backend" even with
`fsGroup` granting group write (0xc0-labs/gitops#418). The orchestrator ran
`chown -R 1000:1000` on the volume's `.minio.sys` and `uploads` on
2026-11-25. A volume restored from a backup older than that needs the same,
by hand, from the node that runs `plane-minio-wl-0`:

```sh
cd /var/lib/kubelet/pods/*/volumes/kubernetes.io~csi/<pv>/mount
chown -R 1000:1000 .minio.sys uploads
```

An init container cannot do it: it would need root, which `restricted`
refuses. RabbitMQ's and Valkey's volumes needed nothing: they started under
their image users.

## The frontends' rate limit

The `web` and `admin` images serve their files with a Caddy that limits each
client address to 300 requests a minute, and a first load with an empty cache
asks for more than that: Plane answered 429 (0xc0-labs/gitops#419). Here every
request reaches Caddy with the load balancer's address, so all users share one
key and no figure is right; limiting requests is the ingress's work. The limit
is removed by mounting another Caddyfile over the image's:
`prod/caddy/web.Caddyfile` and `prod/caddy/admin.Caddyfile`, which are
upstream's minus the `rate_limit` block and the option that orders it.

Every bump of Plane's version compares both files with upstream's
`apps/web/caddy/Caddyfile` and `apps/admin/caddy/Caddyfile` at the new tag.

## Not set up

- Email, the GitHub integration, and any way out to the internet: no
  egress is open, so Plane sends no telemetry either. A pod that cannot start
  without the internet is reported, not given egress.
- Matchmark's own access: its API token, its MCP server, and a
  NetworkPolicy admitting its namespace.
- More than one replica of anything.

Logs go to Loki with every pod's, through the cluster's collector.
Plane exposes no metrics endpoint.

[vault-write]: https://github.com/0xc0-labs/vault/blob/main/README.md#writing-or-rotating-a-secret
