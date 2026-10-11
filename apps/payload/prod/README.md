# payload

Payload CMS (v3), one multi-tenant instance, the backend of several
frontends: the API and media at `https://payload.0xc0.cc`, the admin at
`https://payload.int.0xc0.cc` over WARP only (`httproute.yaml`). The code is
0xc0-labs/payload. One replica: its media sits on a Longhorn RWO volume.

## Before the first sync: the secrets

The operator writes them, as the `vault` repo's README says (values on
stdin, never on a command line):

```sh
export VAULT_ADDR=https://vault.int.0xc0.cc
mise exec -- vault login -no-print

# The database role's password: read by Payload, and by platform/postgres to
# create the role (databases.yaml there). Hex, because it goes into a URL.
openssl rand -hex 24 | mise exec -- vault kv put -mount=apps payload/database password=-
mise exec -- vault kv metadata put -mount=apps \
  -custom-metadata=owner=operator -custom-metadata=rotated_at="$(date +%F)" payload/database

# PAYLOAD_SECRET, which signs sessions and tokens.
openssl rand -base64 48 | tr -d '\n' | mise exec -- vault kv put -mount=apps payload/app payload-secret=-
mise exec -- vault kv metadata put -mount=apps \
  -custom-metadata=owner=operator -custom-metadata=rotated_at="$(date +%F)" payload/app

rm -f ~/.vault-token
```

`platform/postgres/` needs `platform/postgres/superuser` too, as its own
manifests say. Then sync `postgres` in Argo CD, so its databases Job creates
the `payload` database and role, and then `payload`.

## Upgrades

Every push to `main` in 0xc0-labs/payload publishes
`ghcr.io/0xc0-labs/payload`, and its `container-image` job prints the image
as `tag@digest` in its summary. An upgrade is that string in
`deployment.yaml`, in a PR here. The package on GHCR is public, so the
cluster pulls with no secret. With one replica and an RWO volume, a roll
stops the pod before it starts the next: the CMS is down for that moment.
