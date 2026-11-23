# auction-watch

A pod that asks Hetzner's server auction feed every 10 seconds, keeps its own
history of every offer, and mails the operator, through Resend, the offers that
meet his limits or that are far under what their kind usually went for.

- The feed is `https://www.hetzner.com/_resources/app/data/app/live_data_sb.json`,
  the address the auction page's own script fetches. It is public and
  undocumented, and its shape can change without notice: a pass that does not
  know the shape says so (How to read a pass).
- The script is `watch.py`, standard library only, in a ConfigMap built by
  kustomize beside `rules.json`. Its pure functions (the rules, what is mailed)
  are apart from the record, the reading and the sending.
- One pod (`deployment.yaml`, `Recreate`, one replica) with a Longhorn volume
  that holds the record. A CronJob would attach and detach the volume 1,440
  times a day.
- The mail goes through [Resend's Send Email API][resend], from
  `auction-watch@0xc0.cc`.

## How often

The feed is written once a minute, at about second 2 to 4. The pod asks every
10 seconds with `If-Modified-Since`: five of six asks are a 304 with no body and
nothing else happens, and the ask after a write gets the new version, so an
offer is seen within seconds of being published. A new version is a pass, with
one log line.

The interval is `AUCTION_WATCH_POLL_SECONDS` in `deployment.yaml`, never under
5. The liveness probe fails when the loop has not finished an ask for five
minutes.

## The rules

`rules.json`. A pull request edits it.

**Limits**, `limits`, one line each:

| Field | Meaning |
|---|---|
| `name` | what the mail calls the rule |
| `min_ram_gb` | the least RAM, in GB |
| `nvme_required` | `true` when at least one NVMe drive is needed |
| `max_price_eur` | the price must be under it: `Prices.monthly.EUR` as the feed gives it (a month, in EUR, as the feed shows it), and the mail says so |

Today: 64 GB or more with NVMe under 60 EUR, and 128 GB or more with NVMe under
100 EUR.

**History**, `history`:

| Field | Meaning |
|---|---|
| `ram_gb` | the RAM sizes watched, exactly: `[64, 128]` |
| `under_pct` | how far under its usual price an offer is mailed: `20` |
| `window_days` | how far back the usual price looks: `90` |
| `min_offers` | the least history a kind needs to have a baseline: `10` |

**Filters**, `filters`, applied to every rule, the limits and the history alike:

| Field | Meaning |
|---|---|
| `datacenter_prefixes` | the datacenter's name must start with one of them: `["FSN", "NBG"]` (Falkenstein, Nuremberg: Germany; `HEL` is Finland) |
| `skip_hdd_only` | `true` never mails an offer whose only drives are hard disks: at least one HDD, no NVMe and no SATA SSD |

An offer a filter leaves out is still written to the record, which is of the
whole auction, and its kind's baseline does not change: it is only never
mailed. The first mail's cheapest-of-each-shape section follows the filters,
and the pass's log line counts, in `filtered`, the matches they left out.

An offer's **kind** is its CPU, its RAM, each type's drive sizes (sorted) and
whether it has a GPU: two offers of one kind are the same machine. ECC is not
in it, as it follows from the CPU and the RAM. A kind's **baseline** is the
median of the last price of the offers of that kind that went (left the feed)
in the window before now. An offer of a watched RAM is mailed when its price is
at or under the baseline less `under_pct`. A kind with fewer than `min_offers`
offers has no baseline, and only the limits apply to it.

Why exact kinds: by CPU and RAM alone the rule would mail 50 to 100 offers a
week, and by kind a price is typically within 8% of its kind's median. On the
seed, the rule finds about 12 offers a week in the last four weeks (49 offers in 28
days).

Check a change before the pull request:

```sh
curl -sS --compressed https://www.hetzner.com/_resources/app/data/app/live_data_sb.json -o /tmp/feed.json
mise exec -- python3 platform/auction-watch/watch.py --self-test
mise exec -- python3 platform/auction-watch/watch.py --dry-run /tmp/feed.json
```

`--dry-run` loads the seed into a temporary record, reads the saved feed as a
pass would and prints the mail it would send, sending nothing; `--rules FILE`
tries other rules and `--first-pass` adds the "is up" section.

## What is mailed

- One mail a pass at most, for the offers that match a limit or the history
  rule and were not mailed yet, or were mailed at a higher price. An offer
  mailed at this price is not mailed again; one that left the feed is
  forgotten, and mailed again if it comes back.
- The subject names the machine, not the count (a subject with "offer(s) match"
  went to spam): `auction-watch: <CPU>, <RAM> GB RAM, <price> EUR`, the cheapest
  match's, and ` and N more` when there are others. The body's first line says
  the same.
- Each offer: the rules it met (both, in one entry, when it meets a limit and
  the history rule), the price and when it next drops, the CPU, the RAM and
  whether it is ECC, the drives, the datacenter, the specials, and a link that
  opens it: `https://www.hetzner.com/sb/?freetext=<id>`, which the auction
  page reads to show that one offer with its Order button.
- For a history match: how far under it is, the usual price it was compared
  with, how many offers that comes from and from which day to which.
- The first pass of an empty record mails that it is up: how many offers the
  feed holds, and for each limit the cheapest offer of its shape and how far it
  is from the limit.
- After 15 minutes without a read of the feed, one mail; one more when it reads
  again.

## The record

`/data/record.db` on the volume `auction-watch-record` (1Gi: the record is tens
of MB after 400 days), a SQLite file written with Python's `sqlite3`:

- `offers`: for every offer of the feed, whatever its RAM: `id`, `kind`, `ecc`,
  `specials`, `first_seen`, `pod_first_seen`, `last_seen`, `first_price`,
  `min_price`, `last_price`, `gone`, `seed` and `offer` (the feed's entry as it
  was the first time this pod saw it). A row from the seed has `seed = 1`, the
  seed's `first_seen` and no `pod_first_seen`, `specials` or `offer` until a
  read shows it: then it takes the body and specials, is no longer a seed row,
  and `pod_first_seen` says when. An offer that is no longer in the feed is `gone`, and its
  `last_price` is what it went for. A gone offer is deleted 400 days after it
  was last seen.
- `mailed`: the id and price of each offer mailed that is still in the feed.
- `meta`: whether the first mail went, since when the feed cannot be read, the
  feed's `last-modified`, when the seed was loaded, and when the seed rows
  this pod had seen were put right (#617: once, at the first start after it;
  their `pod_first_seen` is the seed's load, the best date left).

It replaces the CronJob's state ConfigMap, its Role and RoleBinding, and the
Kubernetes API in the network policy: the pod mounts no service account token.
When this change was first applied, the record started empty: one "is up" mail,
and every offer that matched mailed once.

### The seed

`seed.jsonl.gz`, 18,422 offers and 268 KB, from [hetzner-radar][radar]'s
published history (`https://static.radar.iodev.org/sb.duckdb`, 2026-08-24 to
2026-11-22, downloaded on 2026-11-22; its code is AGPL-3.0 and it guarantees
nothing about its data). One line an offer: id, kind, ECC, first and last seen,
first, lowest and last price. A second ConfigMap, `auction-watch-seed`, mounts
it in the pod, and the first start loads it into an empty record, marking the
rows `seed = 1` with no `offer`: this record did not see them. The radar's file
holds about half of what the feed shows at a given moment and is often a price
step behind, so it is a seed, not the record.

To make it again (nothing else needs it):

```sh
curl -sSL https://static.radar.iodev.org/sb.duckdb -o /tmp/sb.duckdb
mise exec -- python3 platform/auction-watch/make_seed.py /tmp/sb.duckdb > platform/auction-watch/seed.jsonl.gz
```

It uses the `duckdb` CLI that `mise.toml` pins. Replacing the seed only matters
for a record that is empty.

### Looking at it

```sh
kubectl -n auction-watch exec deploy/auction-watch -- python3 /opt/auction-watch/watch.py --kinds
kubectl -n auction-watch exec deploy/auction-watch -- python3 /opt/auction-watch/watch.py --offer 3117247
```

`--kinds` prints each kind with a baseline: the baseline, how many offers it
comes from and the cheapest live offer of that kind now. `--offer` prints one
offer's record and its kind's baseline.

To copy the file out, a consistent snapshot, not the live file with its log:

```sh
kubectl -n auction-watch exec deploy/auction-watch -- python3 /opt/auction-watch/watch.py --snapshot /tmp/record.db
kubectl -n auction-watch cp $(kubectl -n auction-watch get pod -l app.kubernetes.io/name=auction-watch -o name | cut -d/ -f2):/tmp/record.db ./record.db
```

## How to read a pass

One JSON line for each new version of the feed, in the pod's log; asks that get
a 304 write nothing:

```sh
kubectl -n auction-watch logs deploy/auction-watch --tail=5
```

```json
{"time":"2026-11-22T21:40:03Z","level":"info","msg":"pass","offers":81,"met":0,"history":0,"mailed":0,"feed":"200","seconds":0.1}
```

| Field | Meaning |
|---|---|
| `offers`, `met`, `history`, `mailed` | offers in the feed, offers under a limit, offers far under their usual price, offers in the mail |
| `feed` | `200`, or `unreadable` |
| `error` | why a pass failed: an HTTP status or an error's name, never a response, the key or the address |
| `level` | `warn`: the feed was unreadable, counted from when it began; `error`: Resend refused the mail, so the next ask tries again, with the offers already recorded |

It makes one request an ask, with the client name `auction-watch/1.0` and a 10
second timeout.

## Backup

The volume is PBS's nightly backup of the RKE2 VMs. The record is worth the
weeks of prices gathered since the seed; losing it costs those and a first-pass
mail, and the seed brings back the radar's three months.

## The secret

`platform/auction-watch/mail` in Vault, keys `api_key` (Resend's) and `to` (the
address the mails go to): the operator writes it, as
[`0xc0-labs/vault`'s README][vault-readme] says. The role `auction-watch` and
its policy are in `0xc0-labs/vault`. Neither value is logged or in any file of
this repo.

## Stop it

Set `replicas: 0` on the Deployment in `deployment.yaml`, in a pull request: it
keeps the volume and the record. A `kubectl scale` is put back by Argo CD's
self-heal. To stop it for good, delete `platform/auction-watch/` in a pull
request: the namespace, with the record in it, goes with it.

[resend]: https://resend.com/docs/api-reference/emails/send-email
[radar]: https://static.radar.iodev.org/
[vault-readme]: https://github.com/0xc0-labs/vault/blob/main/README.md
