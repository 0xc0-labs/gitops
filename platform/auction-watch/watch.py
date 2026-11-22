"""Mails the operator the Hetzner auction offers that meet his limits, or that
are far under their usual price (README.md).

A pass reads the feed, writes what it saw into a SQLite record, compares the
offers with the limits and with the record's history, and sends at most one
mail. The pod makes a pass every minute. Standard library only. Logs one JSON
line a pass and never the API key or the address the mail goes to: errors
carry an HTTP status, not a response.

    python watch.py                          the loop the pod runs
    python watch.py --self-test              the assertions
    python watch.py --dry-run FEED [--rules FILE] [--first-pass]
                                             print the mail a saved feed would send,
                                             against the seeded record
    python watch.py --kinds                  each kind with a baseline
    python watch.py --offer ID               one offer's record
    python watch.py --snapshot FILE          a consistent copy of the record
"""

import contextlib
import gzip
import io
import json
import os
import sqlite3
import statistics
import sys
import tempfile
import time
import urllib.error
import urllib.request

FEED_URL = "https://www.hetzner.com/_resources/app/data/app/live_data_sb.json"
AUCTION_PAGE = "https://www.hetzner.com/sb/"
# Resend's "Send Email": https://resend.com/docs/api-reference/emails/send-email
RESEND_URL = "https://api.resend.com/emails"
MAIL_FROM = "auction-watch <auction-watch@0xc0.cc>"
USER_AGENT = "auction-watch/1.0"
TIMEOUT = 10
# How often the feed is asked, with If-Modified-Since: it is written once a
# minute, so most asks are a 304 with no body, and a new version is seen within
# this many seconds. Never under MIN_POLL_SECONDS.
MIN_POLL_SECONDS = 5
POLL_SECONDS = max(MIN_POLL_SECONDS, float(os.environ.get("AUCTION_WATCH_POLL_SECONDS", "10")))
# Seconds without a read of the feed before the operator is told.
ALERT_AFTER_S = 15 * 60
# What a gone offer's row is kept for.
RETENTION_DAYS = 400
HERE = os.path.dirname(os.path.abspath(__file__))
RULES_FILE = os.path.join(HERE, "rules.json")
SEED_FILE = os.environ.get("AUCTION_WATCH_SEED", os.path.join(HERE, "seed.jsonl.gz"))
DB_FILE = os.environ.get("AUCTION_WATCH_DB", "/data/record.db")
HEARTBEAT_FILE = os.environ.get("AUCTION_WATCH_HEARTBEAT", "/tmp/alive")


class FeedError(Exception):
    """The feed could not be read, or has a shape this script does not know."""


class MailError(Exception):
    """Resend refused the mail, or could not be reached."""


def log(**fields):
    fields = {"time": iso(time.time()), "level": "info", **fields}
    print(json.dumps(fields, separators=(",", ":")), flush=True)


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# --- Pure: the feed, the rules, what to mail --------------------------------

def parse_offers(feed):
    """The offers of a feed as flat dicts. Anything unexpected is a FeedError."""
    try:
        offers = []
        for raw in feed["server"]:
            storage = raw["Hardware"]["Storage"]["Details"]
            specials = [str(s) for s in raw["Details"].get("Specials") or []]
            offers.append({
                "id": int(raw["Id"]),
                "cpu": str(raw["Hardware"]["CPU"]["Name"]),
                "ram_gb": float(raw["Hardware"]["RAM"]["Size"]),
                "ecc": bool(raw["Hardware"]["RAM"].get("ecc", False)),
                "nvme": [int(size) for size in storage["nvme"]],
                "sata": [int(size) for size in storage["sata"]],
                "hdd": [int(size) for size in storage["hdd"]],
                "gpu": "GPU" in specials,
                "price": float(raw["Prices"]["monthly"]["EUR"]),
                "datacenter": str(raw["Details"]["Datacenter"]["Name"]),
                "specials": specials,
                "next_drop_s": raw.get("Timer", {}).get("ReduceNext"),
                "raw": raw,
            })
        return offers
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise FeedError(f"unknown feed shape: {type(e).__name__}: {e}") from None


def parse_rules(rules):
    """{"limits": [...], "history": {...}}, checked."""
    try:
        limits = [{"name": str(r["name"]), "min_ram_gb": float(r["min_ram_gb"]),
                   "nvme_required": bool(r["nvme_required"]), "max_price_eur": float(r["max_price_eur"])}
                  for r in rules["limits"]]
        h = rules["history"]
        history = {"ram_gb": [float(x) for x in h["ram_gb"]], "under_pct": float(h["under_pct"]),
                   "window_days": int(h["window_days"]), "min_offers": int(h["min_offers"])}
        if not 0 < history["under_pct"] < 100 or history["window_days"] < 1 or history["min_offers"] < 1:
            raise ValueError("history out of range")
        return {"limits": limits, "history": history}
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"rules file: {type(e).__name__}: {e}") from None


def kind_of(offer):
    """Offers of one kind are the same machine: CPU, RAM, each type's drive
    sizes, GPU or not. ECC follows from the CPU and RAM."""
    drives = [f"{t}:{'+'.join(str(s) for s in sorted(offer[t]))}" for t in ("nvme", "sata", "hdd")]
    return "|".join([offer["cpu"], f"{offer['ram_gb']:g}GB", *drives, "gpu" if offer["gpu"] else "nogpu"])


def has_shape(offer, rule):
    return offer["ram_gb"] >= rule["min_ram_gb"] and (not rule["nvme_required"] or bool(offer["nvme"]))


def meets(offer, rule):
    return has_shape(offer, rule) and offer["price"] < rule["max_price_eur"]


def history_hit(offer, history, base):
    """How far under its usual price an offer is, or None when it is not far
    enough, its RAM is not watched, or its kind has no baseline."""
    if base is None or offer["ram_gb"] not in history["ram_gb"]:
        return None
    median, count, first_day, last_day = base
    if offer["price"] * 100 > median * (100 - history["under_pct"]):
        return None
    return {"median": median, "count": count, "from": first_day, "to": last_day,
            "under_pct": (1 - offer["price"] / median) * 100}


def history_rule_name(history):
    return f"at least {history['under_pct']:g}% under its usual price"


def matches(offers, rules, baseline):
    """[(offer, rules met, history hit or None)] for every offer that meets a
    limit or the history rule. `baseline(kind)` is the kind's baseline or None."""
    found = []
    for offer in offers:
        met = [r["name"] for r in rules["limits"] if meets(offer, r)]
        hit = history_hit(offer, rules["history"], baseline(kind_of(offer))) if offer["ram_gb"] in rules["history"]["ram_gb"] else None
        if hit:
            met.append(history_rule_name(rules["history"]))
        if met:
            found.append((offer, met, hit))
    return found


def plan(found, offers, mailed):
    """What to mail, and the state of `mailed` ({id: price}) once it is mailed.

    An offer is mailed when it matched and was not mailed, or was mailed at a
    higher price. One that left the feed is forgotten.
    """
    to_mail = [(o, met, hit) for o, met, hit in found
               if str(o["id"]) not in mailed or o["price"] < mailed[str(o["id"])]]
    new_mailed = {str(o["id"]): mailed[str(o["id"])] for o in offers if str(o["id"]) in mailed}
    for offer, _, _ in to_mail:
        new_mailed[str(offer["id"])] = offer["price"]
    return to_mail, new_mailed


def eur(value):
    return f"{value:g} EUR"


def duration(seconds):
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return "unknown"
    minutes = int(seconds // 60)
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    return " ".join(p for p in (f"{days}d" if days else "", f"{hours}h" if hours else "", f"{minutes}m") if p)


def drives(offer):
    labels = {"nvme": "NVME", "sata": "SATA SSD", "hdd": "HDD"}
    parts = [f"{sizes.count(size)} x {labels[kind]} {size} GB"
             for kind in ("nvme", "sata", "hdd") for sizes in [offer[kind]] for size in sorted(set(sizes))]
    return ", ".join(parts) or "none listed"


def describe(offer, met, hit):
    lines = [
        f"Offer {offer['id']}: {eur(offer['price'])} a month (Prices.monthly.EUR, as the feed gives it)",
        f"  Rule met: {', '.join(met)}",
    ]
    if hit:
        lines.append(f"  History: {hit['under_pct']:.0f}% under the usual {eur(hit['median'])} of its exact kind "
                     f"(the median last price of {hit['count']} such offers that went from {hit['from']} to {hit['to']})")
    lines += [
        f"  Next price drop in: {duration(offer['next_drop_s'])}",
        f"  CPU: {offer['cpu']}",
        f"  RAM: {offer['ram_gb']:g} GB, {'ECC' if offer['ecc'] else 'not ECC'}",
        f"  Drives: {drives(offer)}",
        f"  Datacenter: {offer['datacenter']}",
        f"  Specials: {', '.join(offer['specials']) or 'none'}",
        f"  Where: {AUCTION_PAGE}?freetext={offer['id']}",
    ]
    return "\n".join(lines)


def cheapest_by_rule(offers, limits):
    lines = []
    for rule in limits:
        shaped = [o for o in offers if has_shape(o, rule)]
        label = rule["name"]
        if not shaped:
            lines.append(f"  {label}: no offer of that shape")
            continue
        best = min(shaped, key=lambda o: o["price"])
        gap = best["price"] - rule["max_price_eur"]
        how = f"{eur(gap)} over the limit" if gap >= 0 else f"{eur(-gap)} under the limit"
        lines.append(f"  {label}: cheapest is offer {best['id']} at {eur(best['price'])}, {how}")
    return lines


def build_mail(to_mail, notices):
    """One mail from the notices of the pass and the offers to report, or None."""
    if not to_mail and not notices:
        return None
    body = list(notices)
    if to_mail:
        body.append(f"{len(to_mail)} offer(s) match. Prices are Prices.monthly.EUR.")
        body.extend(describe(o, met, hit) for o, met, hit in to_mail)
    cheapest = f"{len(to_mail)} offer(s) match, from {eur(min(o['price'] for o, _, _ in to_mail))}" if to_mail else ""
    lead = notices[0].splitlines()[0].rstrip(".") if notices else "auction-watch"
    subject = f"{lead}: {cheapest}" if notices and cheapest else lead if notices else f"{lead}: {cheapest}"
    return subject, "\n\n".join(body) + "\n"


def up_notice(offers, limits):
    return "\n".join([f"auction-watch is up: the feed holds {len(offers)} offers",
                      "The cheapest of each limit's shape:", *cheapest_by_rule(offers, limits)])


def down_notice(minutes):
    return (f"auction-watch cannot read the feed: {minutes} minutes without a read\n"
            "Its log says why; the record was not touched. Another mail comes when it reads again.")


def recovered_notice():
    return "auction-watch reads the feed again"


# --- The record --------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  ecc INTEGER NOT NULL,
  specials TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  first_price REAL NOT NULL,
  min_price REAL NOT NULL,
  last_price REAL NOT NULL,
  gone INTEGER NOT NULL DEFAULT 0,
  seed INTEGER NOT NULL DEFAULT 0,
  offer TEXT
);
CREATE INDEX IF NOT EXISTS offers_kind ON offers (kind, gone, last_seen);
CREATE TABLE IF NOT EXISTS mailed (id INTEGER PRIMARY KEY, price REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Record:
    """Every offer the feed has shown, in one SQLite file, and the state of the
    passes: what was mailed, since when the feed cannot be read, whether the first mail went."""

    def __init__(self, path, history, seed_file=None):
        self.history = history
        self.db = sqlite3.connect(path, timeout=TIMEOUT)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()
        if seed_file and self._meta("seeded") is None:
            self._seed(seed_file)

    def _meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _seed(self, seed_file):
        """Loaded once into an empty record: offers marked as seed, gone until
        the feed shows them, with no offer body: this record did not see them."""
        with gzip.open(seed_file, "rt") as f, self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO offers (id, kind, ecc, first_seen, last_seen, first_price, min_price, last_price, gone, seed)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1)",
                ((int(s["id"]), s["kind"], int(s["ecc"]), s["first"], s["last"], s["first_price"], s["low"], s["price"])
                 for s in map(json.loads, f)))
            self.db.execute("INSERT INTO meta VALUES ('seeded', ?)", (iso(time.time()),))

    def observe(self, offers, now):
        """What a feed read shows: new offers, price changes, offers gone."""
        stamp = iso(now)
        with self.db:
            for o in offers:
                self.db.execute(
                    "INSERT INTO offers (id, kind, ecc, specials, first_seen, last_seen, first_price, min_price, last_price, offer)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (id) DO UPDATE SET last_seen=excluded.last_seen, last_price=excluded.last_price,"
                    " min_price=min(min_price, excluded.min_price), gone=0",
                    (o["id"], kind_of(o), int(o["ecc"]), json.dumps(o["specials"]), stamp, stamp,
                     o["price"], o["price"], o["price"], json.dumps(o.get("raw"), separators=(",", ":"))))
            ids = [o["id"] for o in offers]
            self.db.execute(f"UPDATE offers SET gone=1 WHERE gone=0 AND id NOT IN ({','.join('?' * len(ids))})", ids)
            self.db.execute("DELETE FROM offers WHERE gone=1 AND last_seen<?", (iso(now - RETENTION_DAYS * 86400),))

    def touch(self, now):
        """The feed has not changed (a 304): its offers are still there."""
        with self.db:
            self.db.execute("UPDATE offers SET last_seen=? WHERE gone=0", (iso(now),))

    def baseline(self, kind, now):
        """(median, offers, first day, last day) of the last price of the offers
        of this kind that went in the window before now, or None when there
        are fewer than the least history."""
        rows = self.db.execute(
            "SELECT last_price, last_seen FROM offers WHERE kind=? AND gone=1 AND last_seen>=? AND last_seen<?",
            (kind, iso(now - self.history["window_days"] * 86400), iso(now))).fetchall()
        return self._summarize(rows)

    def _summarize(self, rows):
        if len(rows) < self.history["min_offers"]:
            return None
        days = [r[1][:10] for r in rows]
        return statistics.median(r[0] for r in rows), len(rows), min(days), max(days)

    def baselines(self, now):
        """{kind: baseline} for every kind that has one."""
        by_kind = {}
        for kind, price, seen in self.db.execute(
                "SELECT kind, last_price, last_seen FROM offers WHERE gone=1 AND last_seen>=? AND last_seen<?",
                (iso(now - self.history["window_days"] * 86400), iso(now))):
            by_kind.setdefault(kind, []).append((price, seen))
        return {k: b for k, rows in by_kind.items() if (b := self._summarize(rows))}

    def load_state(self):
        meta = dict(self.db.execute("SELECT key, value FROM meta"))
        mailed = {str(i): p for i, p in self.db.execute("SELECT id, price FROM mailed")}
        return {"mailed": mailed, "failing_since": float(meta.get("failing_since", "0")), "alerted": meta.get("alerted") == "true",
                "up": meta.get("up") == "true", "last_modified": meta.get("last_modified", "")}

    def save_state(self, state):
        with self.db:
            self.db.execute("DELETE FROM mailed")
            self.db.executemany("INSERT INTO mailed VALUES (?, ?)", ((int(i), p) for i, p in state["mailed"].items()))
            self.db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
                ("failing_since", str(state["failing_since"])), ("alerted", "true" if state["alerted"] else "false"),
                ("up", "true" if state["up"] else "false"), ("last_modified", state["last_modified"])])

    def snapshot(self, path):
        target = sqlite3.connect(path)
        with target:
            self.db.backup(target)
        target.close()


# --- Impure: the feed, the mail ----------------------------------------------

def fetch_feed(last_modified):
    """(status, feed or None, last-modified). 304 when the feed has not changed."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "identity"}
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = urllib.request.Request(FEED_URL, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as r:  # nosemgrep: dynamic-urllib-use-detected - FEED_URL is a constant https address
            return r.status, json.loads(r.read()), r.headers.get("Last-Modified", "")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, None, last_modified
        raise FeedError(f"feed answered HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise FeedError(f"feed unreachable or not JSON: {type(e).__name__}") from None


def send_mail(api_key, to, subject, text):
    body = json.dumps({"from": MAIL_FROM, "to": [a.strip() for a in to.split(",") if a.strip()],
                       "subject": subject, "text": text}).encode()
    request = urllib.request.Request(RESEND_URL, data=body, method="POST", headers={
        "Authorization": "Bearer " + api_key, "Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT):  # nosemgrep: dynamic-urllib-use-detected - RESEND_URL is a constant https address
            return
    except urllib.error.HTTPError as e:
        raise MailError(f"Resend answered HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise MailError(f"Resend unreachable: {type(e).__name__}") from None


def run_pass(record, rules, mail, now=None):
    """One pass. `mail(subject, text)` sends. Returns the fields of its log line."""
    started = time.monotonic()
    now = time.time() if now is None else now
    summary = {"msg": "pass", "offers": 0, "met": 0, "history": 0, "mailed": 0, "feed": "error"}
    try:
        state = record.load_state()
        first = not state["up"]
        try:
            status, feed, last_modified = fetch_feed(state["last_modified"])
            offers = parse_offers(feed) if feed is not None else None
        except FeedError as e:
            summary.update(level="warn", error=str(e), feed="unreadable")
            state["failing_since"] = state["failing_since"] or now
            if now - state["failing_since"] >= ALERT_AFTER_S and not state["alerted"]:
                mail("auction-watch: cannot read the feed", down_notice(round((now - state["failing_since"]) / 60)) + "\n")
                state["alerted"] = True
            record.save_state(state)
            return summary
        summary["feed"] = str(status)
        if offers is None and state["up"] and not state["alerted"] and not state["failing_since"]:
            record.touch(now)                      # nothing changed: no pass, no line
            return summary
        notices = []
        if state["alerted"]:
            notices.append(recovered_notice())
        if first:
            if offers is None:
                # Not up yet means no stored last-modified: never a 304.
                raise ValueError("a 304 before the first pass")
            notices.append(up_notice(offers, rules["limits"]))
        to_mail = []
        if offers is None:
            record.touch(now)
        else:
            # Written before the mail: a mail that fails must not cost the history.
            record.observe(offers, now)
            cache = {}
            found = matches(offers, rules, lambda kind: cache.setdefault(kind, record.baseline(kind, now)))
            summary["offers"] = len(offers)
            summary["met"] = sum(1 for o in offers if any(meets(o, r) for r in rules["limits"]))
            summary["history"] = sum(1 for _, _, hit in found if hit)
            to_mail, state["mailed"] = plan(found, offers, state["mailed"])
        message = build_mail(to_mail, notices)
        if message:
            mail(*message)
        summary["mailed"] = len(to_mail)
        state.update(failing_since=0, alerted=False, up=True, last_modified=last_modified)
        record.save_state(state)
        return summary
    except (MailError, urllib.error.URLError, OSError, ValueError, KeyError, sqlite3.Error) as e:
        summary.update(level="error", error=f"{type(e).__name__}: {e}" if not isinstance(e, MailError) else str(e), failed=True)
        return summary
    finally:
        summary["seconds"] = round(time.monotonic() - started, 2)


def loop(record, rules, mail):
    """Asks the feed every POLL_SECONDS for ever; a changed feed is a pass, with
    its log line. The heartbeat file is what the pod's liveness probe reads: its
    age is the time since the last ask ended."""
    while True:
        started = time.monotonic()
        summary = run_pass(record, rules, mail)
        if summary["feed"] != "304":
            log(**summary)
        with open(HEARTBEAT_FILE, "w"):
            pass
        time.sleep(max(1.0, POLL_SECONDS - (time.monotonic() - started)))


# --- Looking at the record ---------------------------------------------------

def show_kinds(record, now):
    live = dict(record.db.execute("SELECT kind, min(last_price) FROM offers WHERE gone=0 GROUP BY kind"))
    rows = sorted(record.baselines(now).items(), key=lambda kv: (-kv[1][1], kv[0]))
    print(f"{len(rows)} kinds with a baseline (the median last price of the offers of the kind that went in the last "
          f"{record.history['window_days']} days, from at least {record.history['min_offers']})")
    print("baseline_eur\toffers\tcheapest_now_eur\tkind")
    for kind, (median, count, _, _) in rows:
        cheapest = f"{live[kind]:g}" if kind in live else "-"
        print(f"{median:g}\t{count}\t{cheapest}\t{kind}")


def show_offer(record, offer_id, now):
    cur = record.db.execute("SELECT * FROM offers WHERE id=?", (offer_id,))
    row = cur.fetchone()
    if row is None:
        print(f"offer {offer_id}: not in the record")
        return 1
    fields = dict(zip([c[0] for c in cur.description], row))
    body = fields.pop("offer")
    for key, value in fields.items():
        print(f"{key}: {value}")
    base = record.baseline(fields["kind"], now)
    print("baseline: " + (f"{base[0]:g} EUR from {base[1]} offers, {base[2]} to {base[3]}" if base else "none (too little history)"))
    print("offer as first seen: " + ("not seen by this record (seed)" if body is None else body))
    return 0


# --- Self-test and dry run ---------------------------------------------------

def _offer(id_, ram, nvme, price, cpu="CPU", gpu=False):
    return {"id": id_, "cpu": cpu, "ram_gb": ram, "ecc": False, "nvme": nvme, "sata": [], "hdd": [], "gpu": gpu,
            "price": price, "datacenter": "FSN1-DC1", "specials": [], "next_drop_s": 3600, "raw": {"Id": id_}}


def _seed_file(path, rows):
    with gzip.open(path, "wt") as f:
        for id_, kind, first, last, low, price in rows:
            f.write(json.dumps({"id": id_, "kind": kind, "ecc": False, "first": first, "last": last,
                                "first_price": price, "low": low, "price": price}) + "\n")


def self_test():
    global fetch_feed
    rules = parse_rules(json.load(open(RULES_FILE)))
    r64, r128 = rules["limits"]
    history = rules["history"]
    assert len(rules["limits"]) == 2
    assert (r64["min_ram_gb"], r64["nvme_required"], r64["max_price_eur"]) == (64, True, 60)
    assert (r128["min_ram_gb"], r128["nvme_required"], r128["max_price_eur"]) == (128, True, 100)
    assert history == {"ram_gb": [64.0, 128.0], "under_pct": 20.0, "window_days": 90, "min_offers": 10}
    pct_name = history_rule_name(history)

    # The limits: under, never equal; NVMe is needed; RAM is a floor.
    assert meets(_offer(1, 64, [512], 59.99), r64)
    assert not meets(_offer(1, 64, [512], 60), r64)
    assert not meets(_offer(1, 64, [], 30), r64)
    assert not meets(_offer(1, 32, [512], 30), r64)
    assert meets(_offer(1, 256, [512], 99), r128) and not meets(_offer(1, 64, [512], 99), r128)

    # A kind is CPU, RAM, each type's sizes sorted, and GPU; ECC and price are not in it.
    a = _offer(1, 64, [512, 1000], 50)
    b = _offer(2, 64, [1000, 512], 90)
    b["ecc"] = True
    assert kind_of(a) == kind_of(b) == "CPU|64GB|nvme:512+1000|sata:|hdd:|nogpu"
    assert kind_of(_offer(3, 64, [512, 1000], 50, gpu=True)) != kind_of(a)
    assert kind_of(_offer(3, 64, [512], 50)) != kind_of(a) and kind_of(_offer(3, 128, [512, 1000], 50)) != kind_of(a)
    assert kind_of(_offer(3, 64, [512, 1000], 50, cpu="Other")) != kind_of(a)

    # The threshold's edge: at or under 80 of a usual 100 is mailed; a cent over is not.
    base = (100.0, 12, "2026-09-01", "2026-11-20")
    assert history_hit(_offer(1, 64, [], 80), history, base)
    assert not history_hit(_offer(1, 64, [], 80.01), history, base)
    hit = history_hit(_offer(1, 128, [], 50), history, base)
    assert round(hit["under_pct"]) == 50 and hit["count"] == 12
    assert not history_hit(_offer(1, 32, [], 10), history, base)      # RAM not watched
    assert not history_hit(_offer(1, 256, [], 10), history, base)
    assert not history_hit(_offer(1, 64, [], 10), history, None)      # no baseline

    # The record: seeded once, marked as seed, and the baseline from the window.
    now = 1_800_000_000
    day = 86400
    kind = kind_of(_offer(0, 64, [512], 0))
    other = kind_of(_offer(0, 64, [1000], 0))
    with tempfile.TemporaryDirectory() as tmp:
        seed = os.path.join(tmp, "seed.jsonl.gz")
        rows = [(100 + i, kind, iso(now - (50 + i) * day), iso(now - (40 + i) * day), 70, 80 + i) for i in range(10)]
        rows.append((200, kind, iso(now - 120 * day), iso(now - 100 * day), 10, 10))        # outside the window
        rows += [(300 + i, other, iso(now - 50 * day), iso(now - 40 * day), 70, 80) for i in range(9)]  # one short
        _seed_file(seed, rows)
        path = os.path.join(tmp, "record.db")
        record = Record(path, history, seed)
        assert record.db.execute("SELECT count(*), sum(seed), sum(gone), count(offer) FROM offers").fetchone() == (20, 20, 20, 0)
        base = record.baseline(kind, now)
        assert base == (84.5, 10, iso(now - 49 * day)[:10], iso(now - 40 * day)[:10]), base
        assert record.baseline(other, now) is None
        assert set(record.baselines(now)) == {kind}
        record.db.execute("DELETE FROM offers WHERE id=109")
        record.db.commit()
        assert record.baseline(kind, now) is None           # nine are too few
        record.db.close()
        record = Record(path, history, seed)                # a second start does not load it again
        assert record.db.execute("SELECT count(*) FROM offers").fetchone() == (19,)

        # Writes: a new offer, a drop, a rise, an offer going, and coming back.
        record.observe([_offer(1, 64, [512], 70)], now)
        row = lambda: record.db.execute(
            "SELECT first_seen, last_seen, first_price, min_price, last_price, gone, seed, offer FROM offers WHERE id=1").fetchone()
        assert row() == (iso(now), iso(now), 70, 70, 70, 0, 0, '{"Id":1}')
        record.observe([_offer(1, 64, [512], 60)], now + 60)
        assert row()[:6] == (iso(now), iso(now + 60), 70, 60, 60, 0)
        record.observe([_offer(1, 64, [512], 65)], now + 120)
        assert row()[:6] == (iso(now), iso(now + 120), 70, 60, 65, 0)
        record.touch(now + 180)
        assert row()[1] == iso(now + 180)
        record.observe([_offer(2, 64, [512], 99)], now + 240)
        assert row()[1:6] == (iso(now + 180), 70, 60, 65, 1)    # gone: its last price is what it went for
        assert record.baseline(kind, now + 241)[1] == 10      # and it joins the history: nine of the seed and this one
        record.observe([_offer(1, 64, [512], 66), _offer(2, 64, [512], 99)], now + 300)
        assert row()[5] == 0
        # Nothing is deleted before 400 days; then a gone offer's row goes.
        record.observe([_offer(2, 64, [512], 99)], now + 301)
        assert row() is not None
        record.observe([_offer(2, 64, [512], 99)], now + 401 * day)
        assert row() is None
        record.db.close()

    # Plans: history alone, both rules in one entry, mailed once, again only lower.
    cheap = _offer(1, 64, [512], 55)             # under 60 and 20% under 84.5? 84.5*0.8 = 67.6: both
    far = _offer(2, 64, [512], 66)               # history only
    near = _offer(3, 64, [512], 70)              # neither
    big = _offer(4, 128, [512], 95)              # a limit only: no baseline for its kind
    usual = lambda k: (84.5, 10, "2026-09-01", "2026-11-20") if k == kind else None
    offers = [cheap, far, near, big]
    found = matches(offers, rules, usual)
    assert [(o["id"], met) for o, met, _ in found] == [
        (1, [r64["name"], pct_name]), (2, [pct_name]), (4, [r128["name"]])]
    assert found[0][2] and found[1][2] and found[2][2] is None
    to_mail, mailed = plan(found, offers, {})
    assert [o["id"] for o, _, _ in to_mail] == [1, 2, 4] and mailed == {"1": 55, "2": 66, "4": 95}
    to_mail, mailed = plan(matches(offers, rules, usual), offers, mailed)
    assert to_mail == [] and mailed == {"1": 55, "2": 66, "4": 95}
    lower = [_offer(1, 64, [512], 50), _offer(2, 64, [512], 67)]
    to_mail, mailed = plan(matches(lower, rules, usual), lower, mailed)
    assert [o["id"] for o, _, _ in to_mail] == [1] and mailed == {"1": 50, "2": 66}
    to_mail, mailed = plan(matches([], rules, usual), [_offer(2, 64, [512], 66)], mailed)
    assert to_mail == [] and mailed == {"2": 66}           # one that left is forgotten
    to_mail, _ = plan(matches([far], rules, usual), [far], mailed)
    assert to_mail == []
    to_mail, _ = plan(matches([cheap], rules, usual), [cheap], mailed)
    assert [o["id"] for o, _, _ in to_mail] == [1]          # and mailed again if it comes back

    # The mail says what the operator needs to decide.
    text = build_mail(found[:1], [])[1]
    for needle in ("Offer 1", "55 EUR", r64["name"], pct_name, "35% under the usual 84.5 EUR", "10 such offers",
                   "2026-09-01 to 2026-11-20", "1h", "64 GB", "not ECC", "1 x NVME 512 GB", "FSN1-DC1", AUCTION_PAGE + "?freetext=1"):
        assert needle in text, needle
    assert "History:" not in build_mail(found[2:], [])[1]
    mixed = _offer(8, 64, [512], 55)
    mixed["sata"], mixed["hdd"] = [512, 1000, 512], [4000, 4000]
    assert drives(mixed) == "1 x NVME 512 GB, 2 x SATA SSD 512 GB, 1 x SATA SSD 1000 GB, 2 x HDD 4000 GB"
    assert drives(_offer(9, 64, [], 55)) == "none listed"
    assert build_mail([], []) is None
    up = up_notice([_offer(1, 64, [512], 65)], rules["limits"])
    assert "holds 1 offers" in up and "5 EUR over the limit" in up and "no offer of that shape" in up

    # A feed it does not know is an error, not an empty list.
    for bad in ({}, {"server": [{"Id": 1}]}, {"server": None}):
        try:
            parse_offers(bad)
            raise AssertionError("accepted")
        except FeedError:
            pass

    # Passes, against a feed and a mailbox in memory.
    sent = []
    good = {"server": [{"Id": 1, "Hardware": {"CPU": {"Name": "c"}, "RAM": {"Size": 64},
                                              "Storage": {"Details": {"nvme": [512], "sata": [], "hdd": []}}},
                        "Prices": {"monthly": {"EUR": 55}}, "Details": {"Datacenter": {"Name": "d"}}}]}
    feed = {"answer": ("200", good, "t1")}

    def fake_fetch(last_modified):
        answer_kind, value, stamp = feed["answer"]
        if answer_kind == "error":
            raise FeedError("down")
        return int(answer_kind), value, stamp
    fetch_feed = fake_fetch

    def mail(subject, text):
        sent.append(subject)

    with tempfile.TemporaryDirectory() as tmp:
        store = Record(os.path.join(tmp, "record.db"), history)
        result = run_pass(store, rules, mail, now)       # the first pass: up, with the offer
        assert result["mailed"] == 1 and len(sent) == 1 and "is up" in sent[0] and "1 offer(s)" in sent[0]
        assert store.load_state()["mailed"] == {"1": 55} and result["history"] == 0
        result = run_pass(store, rules, mail, now + 60)  # nothing new
        assert result["mailed"] == 0 and len(sent) == 1
        feed["answer"] = ("304", None, "t1")
        assert run_pass(store, rules, mail, now + 120)["feed"] == "304"      # the loop logs none of these
        assert len(sent) == 1
        feed["answer"] = ("error", None, "")
        before = store.load_state()["mailed"]
        # Asked every 10 seconds, the mail comes after fifteen minutes, not fifteen asks.
        for n in range(0, 15 * 6):
            run_pass(store, rules, mail, now + 120 + n * 10)
            assert store.load_state()["mailed"] == before and store.load_state()["failing_since"] == now + 120
        assert len(sent) == 1
        for n in range(15 * 6, 15 * 6 + 20):
            run_pass(store, rules, mail, now + 120 + n * 10)
        assert len(sent) == 2 and "cannot read" in sent[1]
        feed["answer"] = ("200", good, "t2")
        run_pass(store, rules, mail, now + 3000)                  # one more when it reads again
        assert len(sent) == 3 and store.load_state()["failing_since"] == 0 and not store.load_state()["alerted"]

        # A history match, counted in the pass's line and mailed once.
        usual_rows = [(100 + i, kind_of(_offer(0, 64, [512], 0, cpu="c")), iso(now - 80 * day), iso(now - 30 * day), 70, 90)
                      for i in range(10)]
        seed = os.path.join(tmp, "seed.jsonl.gz")
        _seed_file(seed, usual_rows)
        store2 = Record(os.path.join(tmp, "record2.db"), history, seed)
        store2.save_state({"mailed": {}, "failing_since": 0, "alerted": False, "up": True, "last_modified": ""})
        sent.clear()
        good["server"][0]["Prices"]["monthly"]["EUR"] = 70       # over the limit, 22% under 90
        result = run_pass(store2, rules, mail, now)
        assert result["history"] == 1 and result["met"] == 0 and result["mailed"] == 1 and "1 offer(s) match" in sent[0]
        run_pass(store2, rules, mail, now + 60)
        assert len(sent) == 1
        good["server"][0]["Prices"]["monthly"]["EUR"] = 69
        run_pass(store2, rules, mail, now + 120)
        assert len(sent) == 2

        # Resend refusing the mail fails the pass: the mailed state stays, and the history is kept.
        good["server"][0]["Prices"]["monthly"]["EUR"] = 55

        def refusing(subject, text):
            raise MailError("Resend answered HTTP 403")
        store3 = Record(os.path.join(tmp, "record3.db"), history)
        store3.save_state({"mailed": {}, "failing_since": 0, "alerted": False, "up": True, "last_modified": "old"})
        result = run_pass(store3, rules, refusing, now)
        state = store3.load_state()
        assert result.get("failed") and state["mailed"] == {} and state["failing_since"] == 0 and state["last_modified"] == "old"
        assert store3.db.execute("SELECT count(*) FROM offers").fetchone() == (1,)

        # The looks.
        snap = os.path.join(tmp, "snap.db")
        store2.snapshot(snap)
        assert sqlite3.connect(snap).execute("SELECT count(*) FROM offers").fetchone() == (11,)
        with contextlib.redirect_stdout(io.StringIO()) as shown:
            show_kinds(store2, now + 3600)
            assert show_offer(store2, 1, now) == 0 and show_offer(store2, 100, now) == 0 and show_offer(store2, 5, now) == 1
        assert "90\t10\t69\t" in shown.getvalue() and "not seen by this record (seed)" in shown.getvalue()
        assert POLL_SECONDS >= MIN_POLL_SECONDS
    print("self-test: ok")


def dry_run(feed_path, rules_path, first_pass):
    """What the pod would mail for a saved feed, against the seed alone."""
    rules = parse_rules(json.load(open(rules_path)))
    with open(feed_path) as f:
        offers = parse_offers(json.load(f))
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        record = Record(os.path.join(tmp, "record.db"), rules["history"], SEED_FILE)
        record.observe(offers, now)
        found = matches(offers, rules, lambda kind: record.baseline(kind, now))
        record.db.close()
    to_mail, _ = plan(found, offers, {})
    print(f"{len(offers)} offers: {sum(1 for o in offers if any(meets(o, r) for r in rules['limits']))} meet a limit, "
          f"{sum(1 for _, _, h in found if h)} are far under their usual price, {len(to_mail)} would be mailed", file=sys.stderr)
    notices = [up_notice(offers, rules["limits"])] if first_pass else []
    message = build_mail(to_mail, notices)
    if message is None:
        print("no mail: no offer matches")
        return
    print("Subject:", message[0], "\n")
    print(message[1])


def main():
    args = sys.argv[1:]
    rules = parse_rules(json.load(open(RULES_FILE)))
    if args == ["--self-test"]:
        self_test()
        return 0
    if args[:1] == ["--dry-run"] and len(args) >= 2:
        rules_path = args[args.index("--rules") + 1] if "--rules" in args else RULES_FILE
        dry_run(args[1], rules_path, "--first-pass" in args)
        return 0
    if args == ["--kinds"]:
        show_kinds(Record(DB_FILE, rules["history"]), time.time())
        return 0
    if args[:1] == ["--offer"] and len(args) == 2:
        return show_offer(Record(DB_FILE, rules["history"]), int(args[1]), time.time())
    if args[:1] == ["--snapshot"] and len(args) == 2:
        Record(DB_FILE, rules["history"]).snapshot(args[1])
        return 0
    if args:
        sys.exit(__doc__)
    api_key, to = os.environ["RESEND_API_KEY"], os.environ["MAIL_TO"]
    loop(Record(DB_FILE, rules["history"], SEED_FILE), rules, lambda subject, text: send_mail(api_key, to, subject, text))


if __name__ == "__main__":
    sys.exit(main())
