# mautic

Mautic, one instance: public at `https://mautic.0xc0.cc` for contacts, the
admin at `https://mautic.int.0xc0.cc` over WARP only (`httproute.yaml`).

## Before the first sync: the secrets

The operator writes them, as the `vault` repo's README says (values on
stdin, never on a command line):

```sh
export VAULT_ADDR=https://vault.int.0xc0.cc
mise exec -- vault login -no-print

# The database user's password: read by Mautic, and by platform/mariadb to
# create the user (databases.yaml there).
openssl rand -base64 24 | tr -d '\n' | mise exec -- vault kv put -mount=apps mautic/database password=-
mise exec -- vault kv metadata put -mount=apps \
  -custom-metadata=owner=operator -custom-metadata=rotated_at="$(date +%F)" mautic/database

# The admin's login: a person's, in ops/, read by no machine. Kept in the
# password manager too.
read -rs v && printf '%s' "$v" | mise exec -- vault kv put -mount=ops mautic/admin email=you@example.com password=- ; unset v

rm -f ~/.vault-token
```

Then sync `mariadb` in Argo CD, so its databases Job creates the `mautic`
database and user, and then `mautic`.

## Install, once

Mautic installs from its web installer, over WARP:

1. Open `https://mautic.int.0xc0.cc/installer`. The database step comes
   prefilled from the environment (`MAUTIC_DB_*`): keep it.
2. Create the admin with the login written to `ops/mautic/admin`.
3. Skip the email step: the sending provider comes from Vault (below).
4. In Settings → Configuration → System settings, set the **Site URL** to
   `https://mautic.0xc0.cc`. The installer takes the name it was opened on;
   the links Mautic sends must carry the public one.

cron and worker wait until the install is done, then start on their own.

## Upgrades

A new digest in `deployment.yaml`. web runs the migrations at its start,
before apache listens: the startup probe allows ten minutes. A major version
goes first to its release notes: Mautic only upgrades one major at a time.

## Rotating the database password

The installer writes the password into `config/local.php`, on the volume. So
a rotation takes three steps: the new value in Vault, a sync of `mariadb` (its
Job sets the user's password to Vault's), then `db_password` in
`config/local.php` and a restart of the pod.

## The sending provider

Mautic sends through `MAUTIC_MAILER_DSN`, from `apps/mautic/mailer` (key
`dsn`), a Symfony mailer DSN: `smtp://user:pass@host:587`, or the
provider's own scheme. A real environment variable wins over `local.php`,
so the DSN field in Configuration → Email Settings is not used; the sender's
name and address still are. Writing it is the whole change, and Vault
Secrets Operator restarts the pod:

```sh
read -rs v && printf '%s' "$v" | mise exec -- vault kv put -mount=apps mautic/mailer dsn=- ; unset v
mise exec -- vault kv metadata put -mount=apps \
  -custom-metadata=owner=operator -custom-metadata=rotated_at="$(date +%F)" mautic/mailer
```

Without it, Mautic sends nothing.

## Configuration changes and the queues

The worker's consumers load the configuration once, when they start. They
restart every five minutes (`supervisord.conf`), so a change made in
Configuration reaches the queued emails within five minutes. To make it
immediate:

```sh
kubectl -n mautic exec deploy/mautic -c worker -- \
  su -s /bin/bash www-data -c 'php /var/www/html/bin/console messenger:stop-workers'
```

