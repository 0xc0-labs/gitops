# Plane in the cluster (gitops#416)

## Spec

Plane Community Edition runs as an application of its own: namespace `plane`,
`https://plane.int.0xc0.cc`, WARP only, never public. The contract is the
issue, 0xc0-labs/gitops#416; this file is its plan and tasks.

- Chart `plane-ce` 1.9.1 from `https://helm.plane.so/`, `planeVersion: v1.5.0`,
  inflated by `apps/plane/prod/kustomization.yaml`.
- Database: the shared PostgreSQL (`platform/postgres`), role and database
  `plane`. Valkey, RabbitMQ and the object store are the chart's, each with a
  Longhorn volume.
- No Secret with a value in git: Vault Secrets Operator builds every Secret the
  chart would create.
- Way in: one `HTTPRoute` on the Gateway's `internal` listener.
- Namespace denies by default; opens only what Plane needs. No egress to the
  internet.

## Plan

1. **Secrets.** `VaultStaticSecret` per Vault path, `apps` engine:
   `plane/database`, `plane/app`, `plane/rabbitmq`, `plane/storage`. The chart
   reads a Secret by name through `external_secrets.*_existingSecret`:
   - `app_env_existingSecret`: `DATABASE_URL` and `REDIS_URL`, from
     `plane/database`.
   - `app_keys_existingSecret`: `SECRET_KEY`, `LIVE_SERVER_SECRET_KEY`, from
     `plane/app`. It is `envFrom`'d last in every backend workload.
   - `live_env_existingSecret`: `LIVE_SERVER_SECRET_KEY`, `REDIS_URL`, from
     `plane/app`.
   - `rabbitmq_existingSecret`: `RABBITMQ_DEFAULT_USER`, `RABBITMQ_DEFAULT_PASS`,
     from `plane/rabbitmq`.
   - `doc_store_existingSecret`: the eight keys the chart's own Secret holds,
     from `plane/storage`.
   - `AMQP_URL` holds RabbitMQ's password and the chart reads it from the same
     Secret as `DATABASE_URL`, but a `VaultStaticSecret` reads one path. It is
     composed by a patch on the backend workloads: an `env` entry reading the
     password from RabbitMQ's Secret, and `AMQP_URL` expanding it, which beats
     `envFrom`.
2. **Jobs.** The chart's two Jobs are named by `Release.Revision`, so a version
   change alters a Job's immutable pod template. A patch makes each an Argo CD
   `Sync` hook deleted before its next creation: it runs again on every sync
   and never fails on an immutable field.
3. **Network.** `default-deny` both ways; Traefik to the routed Services'
   ports; Plane's pods to each other; DNS; PostgreSQL. `platform/postgres`
   admits the pods that connect to it.
4. **Postgres.** `VaultStaticSecret` for `apps/plane/database`, its volume and
   mount in `databases.yaml`, the namespace in its NetworkPolicy.
5. **README.** Secrets table, first admin, backups, what is not set up.

## Tasks

- [ ] `bootstrap/prod/projects.yaml`: `plane` destination of `apps`
- [ ] `apps/plane/prod/`: namespace, kustomization, values, vault-secrets,
      httproute, network-policy
- [ ] `platform/postgres`: secret, mount, network policy
- [ ] `apps/plane/README.md`
- [ ] Render and `kubeconform` of `apps/plane/prod` and `platform/postgres`
- [ ] Every image carries a digest
- [ ] Checks: `issue`, `validate`, `pin`, `semgrep`
