# auction-watch

Every minute a CronJob reads Hetzner's server auction feed and mails the
operator, through Resend, the offers that meet his limits.

- The feed is `https://www.hetzner.com/_resources/app/data/app/live_data_sb.json`,
  the address the auction page's own script fetches. It is public and
  undocumented, and its shape can change without notice: a pass that does not
  know the shape says so (How to read a pass).
- The script is `watch.py`, standard library only, in a ConfigMap built by
  kustomize beside `rules.json`. Its pure functions (the rules, what is mailed)
  are apart from the reading and the sending.
- The mail goes through [Resend's Send Email API][resend], from
  `auction-watch@0xc0.cc`.

## The limits

`rules.json`, one rule a line. A pull request edits a line:

| Field | Meaning |
|---|---|
| `name` | what the mail calls the rule |
| `min_ram_gb` | the least RAM, in GB |
| `nvme_required` | `true` when at least one NVMe drive is needed |
| `max_price_eur` | the price must be under it: `Prices.monthly.EUR` as the feed gives it (a month, in EUR, as the feed shows it), and the mail says so |

Today: 64 GB or more with NVMe under 60 EUR, and 128 GB or more with NVMe under
100 EUR.

Check a change before the pull request, on a saved copy of the feed:

```sh
curl -sS --compressed https://www.hetzner.com/_resources/app/data/app/live_data_sb.json -o /tmp/feed.json
python3 platform/auction-watch/watch.py --self-test
python3 platform/auction-watch/watch.py --dry-run /tmp/feed.json --rules platform/auction-watch/rules.json
```

`--dry-run` prints the mail the feed would send, and sends nothing;
`--first-pass` adds the "is up" section.

## What is mailed

- One mail a pass at most, for the offers that meet a rule and were not mailed
  yet, or were mailed at a higher price. An offer mailed at this price is not
  mailed again; one that left the feed is forgotten, and mailed again if it
  comes back.
- Each offer: the rule it met, the price and when it next drops, the CPU, the
  RAM and whether it is ECC, the drives, the datacenter, the specials, and
  where to find it. There is no link to the offer itself: the auction page's
  script (`serverboerse.js`) orders by a `POST` form to Hetzner's Robot, so no
  address opens an offer. The mail gives the auction page, `/sb/`, and the
  offer's id.
- The first pass, with no state, mails that it is up: how many offers the feed
  holds, and for each rule the cheapest offer of its shape and how far it is
  from the limit.
- After 15 passes in a row that cannot read the feed, one mail; one more when it
  reads again.

## The state

A ConfigMap, `auction-watch-state`, which the pod reads and writes through the
Kubernetes API with a `Role` for that alone (`cronjob.yaml`): `mailed` (id to the
price it was mailed at), `failures`, `alerted`, `up` and `last_modified`.

Argo CD leaves it alone because it is in no manifest: the job creates it, so
Argo CD does not compare it, self-heal does not reset it, and a prune does not
see it. Deleting it makes the next pass an "is up" pass, and every offer that
meets a rule is mailed again.

## How to read a pass

One JSON line a pass, in the job's pod log:

```sh
kubectl -n auction-watch logs -l app.kubernetes.io/name=auction-watch --tail=5
```

```json
{"time":"2026-11-22T21:40:01Z","level":"info","msg":"pass","offers":81,"met":0,"mailed":0,"feed":"200","seconds":0.4}
```

| Field | Meaning |
|---|---|
| `offers`, `met`, `mailed` | offers in the feed, offers meeting a rule, offers in the mail |
| `feed` | `200`; `304` when the feed has not changed since its `last-modified`; `unreadable` |
| `error` | why a pass failed: an HTTP status or an error's name, never a response, the key or the address |
| `level` | `warn`: the feed was unreadable, counted, state untouched but the count; `error`: Resend refused the mail, the pass fails and the state stays as it was, so the next pass tries again |

It makes one request a pass, with `If-Modified-Since` and the client name
`auction-watch/1.0`, and a 10 second timeout; a pass that hangs is ended after
50 seconds.

## The secret

`platform/auction-watch/mail` in Vault, keys `api_key` (Resend's) and `to` (the
address the mails go to): the operator writes it, as
[`0xc0-labs/vault`'s README][vault-readme] says. The role `auction-watch` and
its policy are in `0xc0-labs/vault`. Neither value is logged or in any file of
this repo.

## Stop it

Set `suspend: true` on the CronJob in `cronjob.yaml`, in a pull request: it
keeps the state. A `kubectl patch` of it is put back by Argo CD's self-heal.
To stop it for good, delete
`platform/auction-watch/` in a pull request: the namespace, with the state in
it, goes with it.

[resend]: https://resend.com/docs/api-reference/emails/send-email
[vault-readme]: https://github.com/0xc0-labs/vault/blob/main/README.md
