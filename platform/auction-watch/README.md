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
- The record is also served read-only, as CSV, to Grafana's pods inside the
  cluster (The views).

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

**Mail**, `mail`: `true` or `false`; `true` when the key is missing. With
`false` a pass still reads the feed and writes the record, so the 90-day
history has no gap, and computes what it would mail, but calls Resend for
nothing: no offer and no notice. What it would have mailed is not written into
`mailed`, so an offer that still matches is mailed once `mail` is `true`
again. Its log line says `"mail": "off"` and carries `would_mail`, the count
that did not go out, beside `met`. `--dry-run` says when the rules have mail
off. Today `false` (operator decision, 2026-11-25).

**Limits**, `limits`, one line each:

| Field | Meaning |
|---|---|
| `name` | what the mail calls the rule |
| `min_ram_gb` | the least RAM, in GB |
| `nvme_required` | `true` when at least one NVMe drive is needed |
| `max_price_eur` | the price must be under it: `Prices.monthly.EUR` as the feed gives it (a month, in EUR, **without VAT**), and the mail says so |

Today: 64 GB or more with NVMe under 60 EUR, and 128 GB or more with NVMe under
100 EUR, both without VAT.

**VAT**, `vat_pct`: `21` (Spain's). The feed's prices are without VAT (an offer
mailed at 58 EUR showed 70 EUR at Hetzner's checkout, 2026-11-23), so the mail
adds the price with it. The limits and the history rule stay on the price
without VAT.

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
  went to spam): `auction-watch: <CPU>, <RAM> GB RAM, <price> EUR without VAT`,
  the cheapest match's, and ` and N more` when there are others. The body's
  first line says the same, and that the limits are without VAT too.
- Each offer: its price without VAT, with VAT (`vat_pct`), and the same with the
  IPv4 address, which the feed prices apart (`IPPrices.monthly.EUR`; the mail
  says so when the feed gives none); the rules it met (both, in one entry, when
  it meets a limit and the history rule), when it next drops, the CPU, the RAM and
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

```sh
kubectl -n auction-watch exec deploy/auction-watch -- python3 /opt/auction-watch/watch.py --history "Ryzen 7 7700" --by week
kubectl -n auction-watch exec deploy/auction-watch -- python3 /opt/auction-watch/watch.py --history --groups
kubectl -n auction-watch exec deploy/auction-watch -- python3 /opt/auction-watch/watch.py --lifetimes
```

`--kinds` prints each kind with a baseline: the baseline, how many offers it
comes from and the cheapest live offer of that kind now. `--offer` prints one
offer's record and its kind's baseline.

Every view starts with the days the record covers and how many of its offers
came from the seed.

`--history [TEXT] [--by month|week]` prints, for each kind whose name holds
`TEXT` (every kind of 64 or 128 GB without it), a line a period with the median
of the price its offers went for (their last price), the lowest, and how many
offers that is; by month unless told, a week being named by its Monday. The ten
kinds with the most offers are printed, and the output says how many there are.
`--history --groups` does the same for four groups: 64 GB and 128 GB, each with
and without NVMe. On the seed:

```
$ python3 watch.py --history --groups
the record covers 91 days, 2026-08-24 to 2026-11-22; 18422 of its 18422 offers came from the seed
by month: the median and the lowest price the offers went for (their last price), from the offers that went; 4 of 4 groups

64 GB with NVMe: 7826 offers
month	median_eur	lowest_eur	offers
2026-08	83.5	46	454
2026-09	91	43	2012
2026-10	100	37	3109
2026-11	107	37	2251
```

`--lifetimes [TEXT]` prints how long offers stayed in the feed, from first to
last sighting, for the offers this pod saw appear: the seed's rows are not
counted (they were sampled every five minutes), nor the offers of the pod's
first read (they were there before it began). One line a group: how many went,
the time half and three quarters of them went within, the slowest, how many
went in under two minutes, and how many are still live. The groups are the price
it went for (under 50 EUR, 50 to 60, 60 to 100, 100 or more), how far under its
kind's usual price it was (the baseline of today), and each of the operator's
limits (by price and shape; the datacenter filters are not applied). The feed
changes once a minute, so a time under two minutes is one or two reads and
nothing finer is known.

To copy the file out, a consistent snapshot, not the live file with its log:

```sh
kubectl -n auction-watch exec deploy/auction-watch -- python3 /opt/auction-watch/watch.py --snapshot /tmp/record.db
kubectl -n auction-watch cp $(kubectl -n auction-watch get pod -l app.kubernetes.io/name=auction-watch -o name | cut -d/ -f2):/tmp/record.db ./record.db
```

## The views

The record can be read over HTTP from inside the cluster, as CSV, so that
Grafana reads it where it is instead of a copy in another store (Prometheus and
Loki keep 30 days; the record keeps 400).

- **Where**: `http://auction-watch.auction-watch.svc.cluster.local:8080`, a
  ClusterIP `Service` (`service.yaml`). There is no route and no Ingress, and
  `auction-watch-views-from-grafana` (`network-policy.yaml`) admits to port 8080
  only the pods `app.kubernetes.io/name: grafana` of the namespace `monitoring`.
  Nothing else is open, and the egress is as before. There is no authentication:
  the network policy is the door.
- **The port**: `AUCTION_WATCH_VIEWS_PORT`, 8080 by default. Not
  `AUCTION_WATCH_PORT`: the pod gets that name from Kubernetes for the Service,
  and `enableServiceLinks: false` in `deployment.yaml` keeps those variables
  out. A value that is not a port is logged and the watching goes on without
  views.
- **Only these**: `GET` and `HEAD` are answered; any other method is `405`, any
  other path `404`. A path is looked up in a fixed table and a query string is
  ignored: no part of a request reaches SQL or the filesystem as text.
- **Format**: `200`, `text/csv; charset=utf-8`, RFC 4180 (CRLF, a field quoted
  only when it holds a comma, a quote or a line break): a header row, then the
  rows; an empty result is the header alone. Column names are lowercase with
  underscores and carry their unit (`_eur`, `_seconds`, `_gb`, `_pct`). Times are
  RFC 3339 in UTC. A boolean is `true` or `false`; no value is an empty field.
- **A price is the record's own**: the feed's `Prices.monthly.EUR`, a month
  **without VAT**, the price the limits are compared with (`meets` in
  `watch.py`, and the mail says so). The IPv4 address and the 21% VAT are not
  in any view.
- **Cheap**: a version of the feed (its `last-modified`, in `meta`) is read
  once into memory in one read transaction, and every view is computed from that
  until the next version: at most once for each. The response carries the
  version's date as `Last-Modified` and `Cache-Control: max-age=60`. Two columns
  depend on the clock, `feed_age_seconds` and `age_seconds`, so `summary` and
  `live` are rendered at each request from what was read. A feed that sends no
  `Last-Modified` has no version: then they are read again after a minute.
- **Apart from the watching**: a thread of the same process (`serve`), not a
  second container on the same script. The isolation a pass needs is in the
  connection and the bounds, which a container would not add: the views read
  through a connection of their own, opened `mode=ro` with `query_only`, so they
  cannot write, and, the record being in WAL mode, neither wait for the loop's
  writes nor make it wait. A second container would only be a second Python
  inside the same 128Mi. The thread is a daemon and every request ends in its
  own handler: a view that fails is a `500` and a log line (`view failed`, the
  exception's name and the path), never an exception in the loop. A port that
  cannot be bound is logged and the pod goes on watching. The bounds: at most 8
  requests at once (a ninth is closed with a `503`), and a client that sends
  nothing is dropped after 10 seconds. The heartbeat file, the liveness probe
  and the passes are as before.
- **What a full set costs**: measured on a record of the size of 2026-11-28
  (20,044 offers, 1,130 kinds, 96 days, 29 MB with the offers' bodies): reading
  the version takes 0.04 s, the nine views 0.2 s together, the resident memory
  of the process rises by about 24 MB while they are held (the offers' columns,
  not their bodies, and 0.14 MB of CSV), and that is released when the next
  version replaces them. The pod's limit is 128Mi.

Every view reads the offers as they are: the datacenter filters (`FSN`, `NBG`)
do not apply to any of them, only to the mail. A day is a UTC day. An offer
"went" on the day of its `last_seen`, at its `last_price`, as `--history`
counts it. The record's first day and the pod's first read carry what was
already in the feed, so `appeared` is too high there.

| Path | A row is | Columns |
|---|---|---|
| `/views/summary.csv` | the record, one row | `record_from`, `record_to`, `days`, `offers`, `offers_from_seed`, `kinds`, `live`, `feed_read_at` (the last sighting of a live offer), `feed_age_seconds` |
| `/views/limits.csv` | a limit of `rules.json` | `name`, `min_ram_gb`, `nvme_required`, `max_price_eur`, `live_of_that_shape`, `cheapest_live_eur`, `live_under_limit`, `met_last_7_days`, `met_last_30_days` (offers that went at a price that met it) |
| `/views/groups-daily.csv` | a day and a group of `--history --groups` (64 and 128 GB, with and without NVMe), every day of the record | `day`, `group`, `offers` (that went that day), `median_eur`, `lowest_eur`, `live_at_end_of_day` |
| `/views/flow-daily.csv` | a day | `day`, `appeared`, `gone`, `live_at_end_of_day` |
| `/views/limits-daily.csv` | a day and a limit | `day`, `limit`, `offers` (that went meeting it), `lowest_eur`, plus `shape_offers` and `lowest_of_shape_eur`: the same for every offer of its shape that went, whatever its price |
| `/views/lifetimes.csv` | a row of `--lifetimes` | `group`, `offers` (that went), `half_within_seconds`, `three_quarters_within_seconds`, `slowest_seconds`, `under_two_minutes`, `live`, plus `section`: which of its three tables the row is from |
| `/views/live.csv` | an offer in the feed now, the cheapest first | `id`, `cpu`, `ram_gb`, `nvme_gb`, `sata_gb`, `hdd_gb` (the total of that type's drives), `gpu`, `ecc`, `datacenter`, `price_eur`, `first_seen`, `age_seconds`, `baseline_eur` (its kind's, empty when it has none), `under_baseline_pct` (negative when over it), `limit` (the limits it meets, joined with `; `, or empty), `url` (`https://www.hetzner.com/sb/?freetext=<id>`), plus `kind` |
| `/views/kinds.csv` | a kind with a baseline, as `--kinds` | `kind`, `cpu`, `ram_gb`, `baseline_eur`, `offers_in_window`, `live`, `cheapest_live_eur` |
| `/views/datacenters.csv` | a datacenter prefix, from the offers whose body the record kept (not the seed's) | `datacenter`, `live`, `median_live_eur`, `gone_last_30_days`, `median_gone_last_30_days_eur` |
| `/healthz` | `200` when the record can be read, else `503` | |

`live_at_end_of_day` is the offers that had appeared by the end of that day and
had not yet gone; the record's own day, today, counts the offers still in the
feed. A row of `groups-daily` or `limits-daily` for a day when nothing went has
`offers` at `0` and no median or lowest.

To read one by hand, with the pod's own `wget`, which reaches the loopback port
the network policy does not govern:

```sh
kubectl -n auction-watch exec deploy/auction-watch -- wget -qO- http://127.0.0.1:8080/views/summary.csv
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
