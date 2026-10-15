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
   namespace, the database and user (`MAUTIC_DB_*`), and the hostnames. The
   public one is `mautic.<domain>`, on that domain's HTTPS listener of the
   Gateway (`platform/traefik`) and in external-dns's `domainFilters`. The
   admin's is `mautic-<site>.int.0xc0.cc`.
2. In `platform/mariadb`: the site's `VaultStaticSecret`, volume and mount in
   `databases.yaml`, and its namespace in `network-policy.yaml`.
3. In `bootstrap/prod/projects.yaml`, the namespace as a destination of the
   `apps` project.
4. If Payload feeds it, its namespace in `apps/payload/prod/mautic.yaml`.
5. The secrets, below, and the `vault` repo's policy: `mariadb` reads each
   site's `database` by name.

## Before the first sync: the secrets

The operator writes them as [`vault`'s README, Writing or rotating a
secret][vault-write] says, at `apps/mautic-<site>/`:

| Name | Key | Value |
|---|---|---|
| `database` | `password` | generated: `openssl rand -base64 24`. Read by Mautic, and by platform/mariadb to create the user (`databases.yaml` there) |
| `app` | `secret_key` | generated: `openssl rand -hex 32`. Mautic's `secret_key`, read as `MAUTIC_SECRET_KEY` (it wins over `local.php`) |
| `email` | `api_key` | a Resend API key limited to the site's domain (The sending provider, below) |

And, for the first admin, `ops/mautic-<site>/admin` with `email` and
`password`: a person's login, read by no machine, and kept in the password
manager too. The email is not a secret.

Then sync `mariadb` in Argo CD, so its databases Job creates the database and
user, and then `mautic-<site>`.

## Install, once per site

Mautic installs from its web installer, over WARP:

1. Open `https://mautic-<site>.int.0xc0.cc/installer`. The database step
   comes prefilled with the host, port, name and user (`MAUTIC_DB_*`), but
   not the **Database Password**: a password field never shows a value, and
   submitted empty it sends none, so the install fails with `Access denied
   (using password: NO)`. Type the value of `apps/mautic-<site>/database`,
   key `password`, copied to the clipboard without printing it
   (`vault kv get -field=password`, piped to `pbcopy`, after the login in
   [`vault`'s README][vault-write]).
2. Create the admin with the login written to `ops/mautic-<site>/admin`.
3. Skip the email step: the sending provider comes from Vault (below).
4. In Settings → Configuration → System settings, set the **Site URL** to
   the public name. The installer takes the name it was opened on; the links
   Mautic sends must carry the public one.
5. In Settings → Configuration → API Settings, turn on **API enabled** and
   **Enable HTTP basic auth**. Payload's Mautic adapter authenticates with
   HTTP Basic: without them Mautic refuses its calls.
6. Create the API user for Payload, in this order:
   1. Write its password to Vault first, as [`vault`'s README][vault-write]
      says, at `apps/payload/tenants/<tenant>/mautic`: a new path, so `kv
      put`. `url` is `http://mautic.mautic-<site>.svc`, `username` is
      `payload`, and `password` is generated (`openssl rand -base64 24`).
   2. In Settings → Roles, a role `Payload`, not an administrator, with
      what Payload's calls need: Contacts, view, create and edit other
      users' contacts; Segments, view and edit other users' segments.
   3. In Settings → Users, the user `payload`, with that role, an email on
      the site's domain, and the password from Vault, copied as in step 1.

cron and worker wait until the install is done, then start on their own.

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
before apache listens: the startup probe allows ten minutes. A major version
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
