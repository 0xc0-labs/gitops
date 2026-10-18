# mautic

Mautic, one instance per site that turns on marketing automation (operator
decision, 2026-10-13; workspace#168): a contact can belong to several sites,
and one Mautic cannot keep them apart. Each site's mail goes from its own
domain with its own key.

- `base/` is the common part: the web, cron and worker containers in one pod,
  the Apache config, the policies, the routes.
- `<site>/` is a site's overlay, its own `Application` named `mautic-<site>`,
  in the namespace `mautic-<site>` (the ApplicationSet in `bootstrap/prod/`).

| Site | Public, for contacts | Admin, WARP only |
|---|---|---|
| `artistlabco-com` | `https://mautic.artistlabco.com` | `https://mautic-artistlabco-com.int.0xc0.cc` |

The public name serves what contacts reach and answers `403` on every admin
path (`base/httproute.yaml`).

## Adding a site

1. Copy `artistlabco-com/` to `<site>/` and replace its name in the
   namespace, the database and user (`MAUTIC_DB_*`), `SITE_URL` (the public
   name), `PAYLOAD_API_EMAIL` (an address on the site's domain) and the
   hostnames. The public one is `mautic.<domain>`, on that domain's HTTPS
   listener of the Gateway (`platform/traefik`) and in external-dns's
   `domainFilters`. The admin's is `mautic-<site>.int.0xc0.cc`.
2. In `platform/mariadb`: the site's `VaultStaticSecret`, volume and mount in
   `databases.yaml`, and its namespace in `network-policy.yaml`.
3. In `bootstrap/prod/projects.yaml`, the namespace as a destination of the
   `apps` project.
4. If Payload feeds it, its namespace in `apps/payload/prod/mautic.yaml`.
5. The secrets, below, and the `vault` repo's policy: `mariadb` reads each
   site's `database` by name. The install needs nothing else.

## Before the first sync: the secrets

The operator writes them as [`vault`'s README, Writing or rotating a
secret][vault-write] says, at `apps/mautic-<site>/`:

| Name | Key | Value |
|---|---|---|
| `database` | `password` | generated: `openssl rand -base64 24`. Read by Mautic, and by platform/mariadb to create the user (`databases.yaml` there) |
| `app` | `secret_key` | generated: `openssl rand -hex 32`. Mautic's `secret_key`, read as `MAUTIC_SECRET_KEY` (it wins over `local.php`) |
| `email` | `api_key` | a Resend API key limited to the site's domain (The sending provider, below) |
| `admin` | `email`, `password` | the first admin's login, a person's: kept in the password manager too. The email is not a secret. Read by the install, and by the provision container to log in to the API |
| `payload` | `password` | generated: `openssl rand -base64 24`. The password of the API user Payload logs in as |

Then sync `mariadb` in Argo CD, so its databases Job creates the database and
user, and then `mautic-<site>`.

## Install, once per site

Nothing is clicked. On its first start, web finds no `site_url` in
`config/local.php` and runs `bin/console mautic:install` with the database
(`MAUTIC_DB_*`, the password from `database`), `SITE_URL` as the Site URL
and the first admin from `admin`. On an installed Mautic it changes nothing.
Then, on every start:

- **API enabled** and **Enable HTTP basic auth** are set in `local.php`,
  which is where Mautic 7.2.1 reads them (they are not environment
  variables). Payload's Mautic adapter authenticates with HTTP Basic.
- The migrations are checked (below).
- The `provision` container, once web answers on `localhost`, logs in to the
  API as the admin and creates, if absent, the role `Payload` (Contacts:
  view, create and edit other users' contacts; Segments: view and edit other
  users' segments) and the user `payload`, with `PAYLOAD_API_EMAIL` and the
  password of `payload`. A role or user that exists is left as it is: its
  password is not reset. It then idles. A failure (the admin's login
  refused, a lookup or a create answered with an error) is one line in the
  `provision` container's log, in OpenObserve, and the container idles
  instead of exiting, so the site stays up; fix the cause and restart the
  pod to run it again.

The sending provider is not asked for: it comes from Vault (below). cron and
worker wait until the install is done, then start on their own.

What the operator still does, after the sync: in Payload's admin, the tenant's
Mautic integration gets `payload` as the username, the value of
`apps/mautic-<site>/payload` as the password and
`http://mautic.mautic-<site>.svc` as the URL ([payload's README,
Integrations][payload-integrations]).

Rotating the API user's password is a write to Vault, the same value in
Payload, and the new password on the user in Mautic's admin: the container
never resets one that exists.

A failed install is read from web's log, which never holds a secret. The
installer drops what it finds in an empty database, so the install runs with
`--db_backup_tables true`: a retry after a partial install leaves `bak_`
tables to drop by hand.

An install can leave the migrations table out of step with the schema
(0xc0-labs/gitops#293). The installer builds the schema from the entities
and ends by marking every migration as applied (`finalMigrationStep`, Mautic
7.2.1); an earlier release did not create the table before that step, and the
install still reported success ([mautic/mautic#17126][mautic-17126]). On
`mautic-artistlabco-com`, installed from 7.2.1, 6 of 117 were recorded, and
web's first start after the install replayed the rest on a schema that
already held them and crashed. So the install ends with a check, and only on
a site it has just installed: `doctrine:migrations:up-to-date`, and when it
fails, `doctrine:migrations:version --add --all`. That writes rows to the
`migrations` table and changes no other table. It is for a schema the
installer just built, from the image the site runs: on a site that is behind
its image it would skip migrations it needs, which is why an installed site is
never touched. To read the state:

```sh
kubectl -n mautic-<site> exec deploy/mautic -c cron -- \
  php /var/www/html/bin/console doctrine:migrations:status --no-interaction
```

`Executed Migrations` equals `Available Migrations`.

## Retiring the shared instance

`apps/mautic/prod` (`mautic.0xc0.cc`) is gone from the repo and from Argo CD,
but its namespace carries `Prune=false`: it and its volume stay until the
operator deletes them, once nothing in them is needed:

```sh
kubectl delete namespace mautic
mariadb -h mariadb -u root -e 'DROP DATABASE `mautic`; DROP USER `mautic`@`%`;'
```

Its Vault paths `apps/mautic/*` can go with them. external-dns has removed
the record of `mautic.0xc0.cc`.

## Upgrades

A new digest in `base/deployment.yaml`, which upgrades every site at once. web runs the migrations at its start,
before apache listens: the startup probe allows ten minutes. It runs whatever
the migrations table does not record, so the table must be in step before the
first upgrade (Install, above): a site whose install left it short replays
its whole history and fails. A major version
goes first to its release notes: Mautic only upgrades one major at a time.

## Rotating the database password

The installer writes the password into `config/local.php`, on the volume. So
a rotation takes three steps: the new value in Vault, a sync of `mariadb` (its
Job sets the user's password to Vault's), then `db_password` in
`config/local.php` and a restart of the pod.

The app secret is only a write to Vault: Vault Secrets Operator restarts the
pod, and the environment wins over `local.php`.

## The sending provider

Mautic sends through `MAUTIC_MAILER_DSN`, Resend's SMTP
(`smtp://resend:<key>@smtp.resend.com:587`), with the key from
`apps/mautic-<site>/email` (key `api_key`, one per site and limited to its
domain). SMTP only, never a provider's own scheme (the workspace's
[`docs/design/platform.md`][design-platform], email). A real environment
variable wins over `local.php`, so the DSN field in Configuration → Email
Settings is not used; the sender's name and address still are, and must be on
the site's domain. Writing the key, as [`vault`'s README][vault-write] says
for a value shown once, is the whole change, and Vault Secrets Operator
restarts the pod.

Without it, Mautic sends nothing.

## Configuration changes and the queues

The worker's consumers load the configuration once, when they start. They
restart every five minutes (`supervisord.conf`), so a change made in
Configuration reaches the queued emails within five minutes. To make it
immediate:

```sh
kubectl -n mautic-<site> exec deploy/mautic -c worker -- \
  su -s /bin/bash www-data -c 'php /var/www/html/bin/console messenger:stop-workers'
```

[vault-write]: https://github.com/0xc0-labs/vault/blob/main/README.md#writing-or-rotating-a-secret
[design-platform]: https://github.com/0xc0-labs/workspace/blob/main/docs/design/platform.md

[mautic-17126]: https://github.com/mautic/mautic/issues/17126
[payload-integrations]: https://github.com/0xc0-labs/payload/blob/main/README.md#integrations
