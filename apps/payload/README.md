# payload

Payload CMS (v3), one multi-tenant instance per environment, the backend of
several frontends. The code is 0xc0-labs/payload. Two environments, each an
Argo CD application of its own:

| | prod | staging |
|---|---|---|
| Application and namespace | `payload` | `payload-staging` |
| Admin, API, GraphQL | `https://payload.int.0xc0.cc`, over WARP | `https://payload-staging.int.0xc0.cc`, over WARP |
| Public | `https://payload.0xc0.cc/api/media/file/`, the uploaded files alone | nothing |
| Data | its own | the seed, versioned in 0xc0-labs/payload |
| Media volume | 10Gi | 2Gi |
| Vault | `apps/payload/*` | `apps/payload-staging/*` |
| Database | `payload` on platform/postgres | `payload-staging` on platform/postgres |

`base/` holds what both share; `prod/` and `staging/` add the namespace, the
routes, the URL and the pin. One replica each: the media sits on a Longhorn
RWO volume.

## Before the first sync: the secrets

The operator writes them, as the `vault` repo's README says (values on
stdin, never on a command line). Every value ends without a newline (`tr -d
'\n'`). platform/postgres's databases Job reads the password with psql's
backquotes, which drop the trailing newline, while the pod's environment
keeps it: a stored newline makes the role's password and the one Payload
sends differ.

```sh
export VAULT_ADDR=https://vault.int.0xc0.cc
mise exec -- vault login -no-print

for env in payload payload-staging; do
  # The database role's password: read by the environment, and by
  # platform/postgres to create the role (databases.yaml there). Hex, because
  # it goes into a URL.
  openssl rand -hex 24 | tr -d '\n' | mise exec -- vault kv put -mount=apps "$env/database" password=-
  mise exec -- vault kv metadata put -mount=apps \
    -custom-metadata=owner=operator -custom-metadata=rotated_at="$(date +%F)" "$env/database"

  # PAYLOAD_SECRET, which signs sessions and tokens: one per environment.
  openssl rand -base64 48 | tr -d '\n' | mise exec -- vault kv put -mount=apps "$env/app" payload-secret=-
  mise exec -- vault kv metadata put -mount=apps \
    -custom-metadata=owner=operator -custom-metadata=rotated_at="$(date +%F)" "$env/app"
done

# Staging alone: the seed users' password (Staging's data, below), beside
# payload-secret. kv patch adds the key and keeps payload-secret.
openssl rand -base64 24 | tr -d '\n' | mise exec -- vault kv patch -mount=apps payload-staging/app seed-password=-
mise exec -- vault kv metadata put -mount=apps \
  -custom-metadata=owner=operator -custom-metadata=rotated_at="$(date +%F)" payload-staging/app

rm -f ~/.vault-token
```

`kv put` writes a secret whole: run again on `payload-staging/app`, it
drops `seed-password`. A later change to one of its keys goes through `kv
patch`.

The image is private: both environments pull it with `apps/shared/ghcr-pull`,
a token that can only read packages, which Vault Secrets Operator turns into
the `ghcr-pull-vault` pull secret (`base/vault-secrets.yaml`). The `vault`
repo's README writes it. `platform/postgres/` needs `platform/postgres/superuser`
too, as its own manifests say. Then sync `postgres` in Argo CD, so its
databases Job creates each environment's database and role, and then
`payload` and `payload-staging`.

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
They and the super-admin log in with `SEED_PASSWORD`, the `seed-password`
key at `apps/payload-staging/app` (above), which the repo never holds. To
read it:

```sh
mise exec -- vault kv get -mount=apps -field=seed-password payload-staging/app
```

Without the key, the seed of an empty database exits non-zero with its own
message, and the sync shows the hook failed. A new value reaches the users at
the next reset, not before: the seed does not touch a database with data.

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

With one replica and an RWO volume, a new version or a restart stops the pod
before it starts the next: the CMS is down for that moment.
