# payload

Payload CMS (v3), one multi-tenant instance per environment, the backend of
several frontends (operator decision, 2026-10-09; workspace#64). The code is
0xc0-labs/payload. Two environments, each an Argo CD application of its own:

| | prod | staging |
|---|---|---|
| Application and namespace | `payload` | `payload-staging` |
| Admin, API, GraphQL | `https://payload.int.0xc0.cc`, over WARP | `https://payload-staging.int.0xc0.cc`, over WARP |
| Public | nothing of Payload: its media alone, at `https://media.0xc0.cc`, served by Cloudflare | nothing |
| Data | its own | the seed, versioned in 0xc0-labs/payload |
| Media | the R2 bucket `payload-media` | the R2 bucket `payload-staging-media`, served by Payload over WARP |
| Vault | `apps/payload/*` | `apps/payload-staging/*` |
| Database | `payload` on platform/postgres | `payload-staging` on platform/postgres |

`base/` holds what both share; `prod/` and `staging/` add the namespace, the
routes, the URL, the bucket, the email sender's name and the pin. One replica
each, with no volume: a new version or a restart starts the new pod and stops
the old one once the new one is ready (`RollingUpdate`, `maxUnavailable: 0`),
so the CMS keeps serving.

## Before the first sync: the secrets

The operator writes them as [`vault`'s README, Writing or rotating a
secret][vault-write] says, each environment at its own path, `apps/payload/`
and `apps/payload-staging/`:

| Path | Key | Value |
|---|---|---|
| `database` | `password` | generated: `openssl rand -hex 24`, hex because it goes into a URL. Read by the environment, and by platform/postgres to create the role (`databases.yaml` there) |
| `app` | `payload-secret` | generated: `openssl rand -base64 48`, one per environment. It signs sessions and tokens |
| `app`, staging alone | `seed-password` | generated: `openssl rand -base64 24`. The seed's super-admin (Staging's data, below) |
| `media` | `access_key_id`, `secret_access_key` | an R2 token scoped to the environment's bucket (Media, below) |
| `email` | `api_key` | a Resend API key, one per environment (Email, below) |
| `email`, staging alone | `redirect_to` | the inbox staging's email goes to (Email, below) |
| `preview` | `secret` | generated: `openssl rand -base64 32`, the same value as its front's `apps/artistlabco-com(-staging)/preview` (Fronts, below) |
| `resources-storage` | `bucket`, `endpoint`, `access_key_id`, `secret_access_key` | the private resources bucket and a token scoped to it (Resources bucket, below) |
| `tenants/<slug>/<name>` | the keys that tenant's code reads | a tenant's secrets (Tenants, below) |

Where each comes from, and staging's in order, with why `app` takes `kv put`
then `kv patch`: [`vault`'s README, Application delivery: what the operator
writes][vault-delivery], steps 4 to 6.

No value ends in a newline. platform/postgres's databases Job reads the
password with psql's backquotes, which drop it, while the pod's environment
keeps it: a stored newline makes the role's password and the one Payload
sends differ.

The image is private: both environments pull it with `apps/shared/ghcr-pull`,
a token that can only read packages, which Vault Secrets Operator turns into
the `ghcr-pull-vault` pull secret (`base/vault-secrets.yaml`), written as the
same section of `vault`'s README says (step 3). `platform/postgres/` needs
`platform/postgres/superuser` too, as its own manifests say. Then sync
`postgres` in Argo CD, so its databases Job creates each environment's
database and role, and then `payload` and `payload-staging`.

## Media

Each environment keeps `media`'s files in its own Cloudflare R2 bucket,
`<namespace>-media`, created by `infrastructure`'s Cloudflare root. The pod
reads and writes it with a token scoped to that bucket, `access_key_id` and
`secret_access_key` at `apps/<namespace>/media`, which the operator makes in
Cloudflare and writes as [`vault`'s README][vault-delivery] says (step 5).
Each overlay sets the image's `S3_ENDPOINT` and `S3_BUCKET`; the server, the
migrations and the seed all get them, since all three load the same config.

- **Prod** sets `MEDIA_PUBLIC_URL=https://media.0xc0.cc` too: documents point
  there, and Cloudflare serves the bucket through its cache, with `nosniff`
  and a `default-src 'none'` CSP. Payload serves no file, and nothing of it is
  public: its own names are `payload.int.0xc0.cc`, over WARP.
- **Staging**'s bucket has no hostname: Payload serves its files at
  `/api/media/file/`, over WARP.

The way out is HTTPS to Cloudflare's published IPv4 ranges
(`base/network-policy.yaml`, `egress-r2`, with the date they were read).
Changing where files go moves none: what was uploaded to the volume before
stays there.

R2 keeps no copies of its own, and nothing copies the media outside
Cloudflare: an accepted risk, in the workspace's
[`docs/accepted-risks.md`][accepted-risks] (workspace#89).

## Email

The server sends Payload's email through Resend's SMTP server,
`smtp.resend.com:587`, from `noreply@0xc0.cc`: as `0xc0` in prod, as
`0xc0 (staging)` in staging. What it sends, and how the image takes the
settings: [payload's README, Email][payload-email].

- The settings are the `payload-email` ConfigMap (`base/kustomization.yaml`)
  and each environment's `EMAIL_FROM_NAME`. The password, `SMTP_PASS`, is a
  Resend API key, `api_key` at `apps/<namespace>/email`, written as
  [`vault`'s README][vault-delivery] says (step 6). A new key restarts the
  pod.
- The server alone reads them. The migrations and the seed send nothing, so
  they hold neither the key nor `SMTP_HOST`: with one and not the other,
  Payload's config does not load.
- **Staging sends to one inbox.** `EMAIL_REDIRECT_TO`, from `redirect_to` at
  `apps/payload-staging/email`, takes every message instead of its
  recipients, marked as staging's. The address lives in Vault because this
  repo is public. Its key is not optional: without it, staging's new pod does
  not start, so staging never mails a real recipient. Prod sets no redirect.
- The way out is TCP 587 to anything outside the private ranges, for the
  server's pod alone (`base/network-policy.yaml`, `egress-smtp`): Resend's
  server is an AWS load balancer, with no fixed range.

To check it: "Forgot password?" on each environment's admin. Prod's email
reaches the user; staging's, the one inbox.

## Tenants

A tenant's secrets live at `apps/<namespace>/tenants/<slug>/<name>`, and
Vault Secrets Operator syncs each into a Secret that the pod mounts read-only
at `/var/run/secrets/tenants/<slug>/<name>/<key>`, one file per key.
`TENANT_SECRETS_DIR` is `/var/run/secrets/tenants`. Today's tenant is
`artistlab`:

| Name | Keys | Where |
|---|---|---|
| `email` | the tenant's Resend settings | both |
| `stripe` | the tenant's Stripe keys | both |
| `discord` | the tenant's Discord credentials | both |
| `mautic` | `url` (`http://mautic.mautic-artistlabco-com.svc`), `username`, `password`: the site's own Mautic and the API user made for Payload | prod alone |

Each volume is optional, so a rollout never waits on a value Vault does not
hold yet: the files appear once the operator writes the secret and Vault
Secrets Operator syncs it.

Adding a secret to a tenant is one `VaultStaticSecret` in
`base/vault-secrets.yaml` (prod alone: `prod/mautic.yaml`), labelled
`app.kubernetes.io/component: tenant-secret`, which is how the environments'
`replacements` set its path's namespace, and one volume with its mount in
`base/deployment.yaml`. A new tenant is the same blocks under its own slug.

The way out to Stripe is `egress-stripe` (`base/network-policy.yaml`): HTTPS
to Stripe's own ranges. Discord's API sits behind Cloudflare, so `egress-r2`
carries it.

## Resources bucket

The private resources bucket's credentials, `RESOURCES_S3_BUCKET`,
`RESOURCES_S3_ENDPOINT`, `RESOURCES_S3_ACCESS_KEY_ID` and
`RESOURCES_S3_SECRET_ACCESS_KEY`, come from `apps/<namespace>/resources-storage`
and are optional: the pod starts without them. It reaches the bucket over the
same egress as the media (`egress-r2`).

## Fronts

Each environment is called by its own front, a BFF (operator decision,
2026-10-13; workspace#168): `payload` by `artistlabco-com`, `payload-staging`
by `artistlabco-com-staging`, over the cluster network at
`http://payload.<namespace>.svc`. Payload stays private. `front.yaml` admits
HTTP from that one namespace and no other.

Leads reach each site's own Mautic through Payload, never from the front:
prod's pod may reach the pods of `mautic-artistlabco-com`'s `mautic` Service
on 8080 (`mautic.yaml`, and `apps/mautic/base/network-policy.yaml` for the
other end). Staging has no Mautic secret and no way to one.

## Delivery

Every merge to 0xc0-labs/payload's `main` builds the image and cuts a release
candidate, `vX.Y.Z-rc.N`. Each one opens a PR here that changes staging's pin
alone, and that PR merges itself once its checks pass. A release, `vX.Y.Z`,
is run by hand on what staging runs, the same digest: its PR changes prod's
pin, and merging it is prod's approval. The image is never rebuilt between
the two.

The pin is in each environment's `kustomization.yaml`:

```yaml
images:
  - name: ghcr.io/0xc0-labs/payload
    newTag: v0.4.0-rc.1
    digest: sha256:<64 hex>
configMapGenerator:
  - name: release
    literals:
      - APP_VERSION=v0.4.0-rc.1
      - APP_ENVIRONMENT=staging
```

`pin / check` fails when `APP_VERSION` is not the image's `newTag`, and when
a bot's PR changes anything but a pin. Both start as a placeholder, `v0.0.0`
and an all-zero digest: staging waits on its first rc, prod on its first
release, and until then their syncs fail on the image.

Every signal carries the version and the environment, from the `release`
ConfigMap: `OTEL_RESOURCE_ATTRIBUTES=service.version=…,deployment.environment=…`.
The image adds its commit.

### Migrations

Each sync runs the migrations with the image it deploys, before the server
(`base/migrate.yaml`): a Job in the wave before the Deployment's. When it
fails, the sync stops there and the running version keeps serving. Every
migration is expand and contract, so that version works on the new schema.

### Rolling back

Revert the promotion PR: the previous pin comes back, and with it the
previous digest. The migrations are not rolled back; expand and contract is
what lets the previous version run on the newer schema.

## Staging's data

After every sync, once the new version serves, a Job runs the image with
`seed` (`staging/seed.yaml`): it fills an empty database with the seed and
does nothing on one that has data. The seed is tenants and test users alone.
The super-admin logs in with `SEED_PASSWORD`, the `seed-password` key at
`apps/payload-staging/app` (above), which the repo never holds. The editors
get random passwords nobody knows: to test as one, the super-admin sets its
password from the admin. To read `SEED_PASSWORD`:

```sh
mise exec -- vault kv get -mount=apps -field=seed-password payload-staging/app
```

Without the key, the seed of an empty database exits non-zero with its own
message, and the sync shows the hook failed. A new value reaches the
super-admin at the next reset, not before: the seed does not touch a database
with data.

To reset staging to the seed, run the same Job with `seed --reset`, which
empties the database first:

```sh
kubectl -n payload-staging get job payload-seed -o json \
  | jq '{apiVersion, kind, metadata: {name: "payload-seed-reset"},
         spec: {backoffLimit: 0, template: (.spec.template
           | .metadata.labels |= with_entries(select(.key | test("controller-uid|job-name") | not))
           | .spec.containers[0].args = ["seed", "--reset"])}}' \
  | kubectl -n payload-staging create -f -
kubectl -n payload-staging logs -f job/payload-seed-reset
kubectl -n payload-staging delete job payload-seed-reset
```

## Rotating the database password

The new value in Vault, without a newline (above). Vault Secrets Operator
copies it within the hour (`refreshAfter`), to the environment, which
restarts the pod, and to platform/postgres; then sync `postgres`, whose
databases Job sets the role's password to Vault's. Between the two, the pod
cannot connect.

[vault-write]: https://github.com/0xc0-labs/vault/blob/main/README.md#writing-or-rotating-a-secret
[vault-delivery]: https://github.com/0xc0-labs/vault/blob/main/README.md#application-delivery-what-the-operator-writes
[accepted-risks]: https://github.com/0xc0-labs/workspace/blob/main/docs/accepted-risks.md
[payload-email]: https://github.com/0xc0-labs/payload/blob/main/README.md#email
