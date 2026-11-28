"""Mails the operator the Hetzner auction offers that meet his limits, or that
are far under their usual price (README.md).

A pass reads the feed, writes what it saw into a SQLite record, compares the
offers with the limits and with the record's history, and sends at most one
mail. The pod makes a pass every minute. Standard library only. Logs one JSON
line a pass and never the API key or the address the mail goes to: errors
carry an HTTP status, not a response.

    python watch.py                          the loop the pod runs, and the views of the record on :8080
    python watch.py --self-test              the assertions
    python watch.py --dry-run FEED [--rules FILE] [--first-pass]
                                             print the mail a saved feed would send,
                                             against the seeded record
    python watch.py --kinds                  each kind with a baseline
    python watch.py --offer ID               one offer's record
    python watch.py --history [TEXT] [--by month|week] [--groups]
                                             the prices offers went for, a period at a time
    python watch.py --lifetimes [TEXT]       how long offers stayed in the feed
    python watch.py --snapshot FILE          a consistent copy of the record
"""

import calendar
import contextlib
import csv
import datetime
import email.utils
import gzip
import http.client
import http.server
import io
import json
import math
import os
import re
import socket
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
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
# How many kinds `--history` prints: the ones with the most offers.
HISTORY_KINDS = 10
# An offer that went in less than this was there for one read of the feed or two.
QUICK_SECONDS = 120
# The views of the record (README.md, The views): the port, how long a client may cache one, how many
# requests are answered at once, and how long a silent client is waited for.
SERVE_PORT = int(os.environ.get("AUCTION_WATCH_PORT", "8080"))
VIEW_CACHE_SECONDS = 60
MAX_CONNECTIONS = 8
REQUEST_TIMEOUT = 10
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

def _ip_price(raw):
    """What the IPv4 address costs a month, apart from the price, or None when the feed does not say."""
    try:
        return float(raw["IPPrices"]["monthly"]["EUR"])
    except (KeyError, TypeError, ValueError):
        return None


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
                "ip_price": _ip_price(raw),
                "datacenter": str(raw["Details"]["Datacenter"]["Name"]),
                "specials": specials,
                "next_drop_s": raw.get("Timer", {}).get("ReduceNext"),
                "raw": raw,
            })
        return offers
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise FeedError(f"unknown feed shape: {type(e).__name__}: {e}") from None


def parse_rules(rules):
    """{"limits": [...], "history": {...}, "filters": {...}, "vat_pct": n, "mail": bool}, checked."""
    try:
        mail = rules.get("mail", True)
        if not isinstance(mail, bool):
            raise ValueError("mail must be true or false")
        limits = [{"name": str(r["name"]), "min_ram_gb": float(r["min_ram_gb"]),
                   "nvme_required": bool(r["nvme_required"]), "max_price_eur": float(r["max_price_eur"])}
                  for r in rules["limits"]]
        h = rules["history"]
        history = {"ram_gb": [float(x) for x in h["ram_gb"]], "under_pct": float(h["under_pct"]),
                   "window_days": int(h["window_days"]), "min_offers": int(h["min_offers"])}
        if not 0 < history["under_pct"] < 100 or history["window_days"] < 1 or history["min_offers"] < 1:
            raise ValueError("history out of range")
        f = rules["filters"]
        filters = {"datacenter_prefixes": [str(p) for p in f["datacenter_prefixes"]], "skip_hdd_only": bool(f["skip_hdd_only"])}
        vat_pct = float(rules["vat_pct"])
        if not 0 <= vat_pct < 100:
            raise ValueError("vat_pct out of range")
        return {"limits": limits, "history": history, "filters": filters, "vat_pct": vat_pct, "mail": mail}
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


def allowed(offer, filters):
    """Whether an offer may be mailed at all: in one of the datacenters wanted,
    and not with hard disks as its only drives. The record keeps the rest."""
    if filters["datacenter_prefixes"] and not offer["datacenter"].startswith(tuple(filters["datacenter_prefixes"])):
        return False
    return not (filters["skip_hdd_only"] and offer["hdd"] and not offer["nvme"] and not offer["sata"])


def matches(offers, rules, baseline, filtered=True):
    """[(offer, rules met, history hit or None)] for every offer that meets a
    limit or the history rule. `baseline(kind)` is the kind's baseline or None.
    The filters apply to every rule; `filtered=False` skips them, to count what they leave out."""
    found = []
    for offer in offers:
        if filtered and not allowed(offer, rules["filters"]):
            continue
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
    return f"{round(value, 2):g} EUR"


def eur_cents(value):
    return f"{round(value + 1e-9, 2):.2f} EUR"


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


def describe(offer, met, hit, vat_pct):
    """The feed's prices are without VAT: the mail also gives what the checkout shows."""
    with_vat = 1 + vat_pct / 100
    ip = offer["ip_price"]
    ip_line = ("  With the IPv4 address: its price is not in the feed" if ip is None else
               f"  With the IPv4 address ({eur(ip)} without VAT): {eur(offer['price'] + ip)} without VAT, "
               f"{eur_cents((offer['price'] + ip) * with_vat)} with VAT")
    lines = [
        f"Offer {offer['id']}: {eur(offer['price'])} a month without VAT (Prices.monthly.EUR, as the feed gives it)",
        f"  With {vat_pct:g}% VAT: {eur_cents(offer['price'] * with_vat)}",
        ip_line,
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


def machine_line(to_mail):
    """The cheapest match as a machine and a price: an advert's subject went to spam (2026-11-23)."""
    best = min((o for o, _, _ in to_mail), key=lambda o: o["price"])
    more = f" and {len(to_mail) - 1} more" if len(to_mail) > 1 else ""
    return f"{best['cpu']}, {best['ram_gb']:g} GB RAM, {eur(best['price'])} without VAT{more}"


def build_mail(to_mail, notices, vat_pct):
    """One mail from the notices of the pass and the offers to report, or None."""
    if not to_mail and not notices:
        return None
    body = list(notices)
    if to_mail:
        body.append(f"{machine_line(to_mail)}. Prices are Prices.monthly.EUR, without VAT, and so are the limits; "
                    f"the checkout adds {vat_pct:g}%.")
        body.extend(describe(o, met, hit, vat_pct) for o, met, hit in to_mail)
    cheapest = machine_line(to_mail) if to_mail else ""
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
  pod_first_seen TEXT,
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
        if "pod_first_seen" not in {r[1] for r in self.db.execute("PRAGMA table_info(offers)")}:
            self.db.execute("ALTER TABLE offers ADD COLUMN pod_first_seen TEXT")
        self.db.commit()
        if seed_file and self._meta("seeded") is None:
            self._seed(seed_file)
        self._repair_seed_rows()

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
            self.db.executemany("INSERT INTO meta VALUES (?, ?)", [("seeded", iso(time.time())), ("seed_rows_repaired", "new")])

    def _repair_seed_rows(self):
        """Once: a seed row this pod saw before #617 kept the seed's mark. Its last
        sighting is after the seed was loaded; the first start is the best date for its first."""
        if self._meta("seed_rows_repaired") is not None:
            return
        seeded = self._meta("seeded")
        with self.db:
            if seeded is not None:
                self.db.execute("UPDATE offers SET seed=0, pod_first_seen=? WHERE seed=1 AND last_seen>=?", (seeded, seeded))
            self.db.execute("INSERT INTO meta VALUES ('seed_rows_repaired', ?)", (iso(time.time()),))

    def observe(self, offers, now):
        """What a feed read shows: new offers, price changes, offers gone."""
        stamp = iso(now)
        with self.db:
            # Every offer is gone unless this read shows it: the upserts bring back the rest.
            self.db.execute("UPDATE offers SET gone=1 WHERE gone=0")
            for o in offers:
                self.db.execute(
                    "INSERT INTO offers (id, kind, ecc, specials, first_seen, pod_first_seen, last_seen, first_price, min_price, last_price, offer)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (id) DO UPDATE SET last_seen=excluded.last_seen, last_price=excluded.last_price,"
                    " min_price=min(min_price, excluded.min_price), gone=0, seed=0,"
                    " specials=coalesce(specials, excluded.specials), offer=coalesce(offer, excluded.offer),"
                    " pod_first_seen=coalesce(pod_first_seen, excluded.pod_first_seen)",
                    (o["id"], kind_of(o), int(o["ecc"]), json.dumps(o["specials"]), stamp, stamp, stamp,
                     o["price"], o["price"], o["price"], json.dumps(o.get("raw"), separators=(",", ":"))))
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
    """One pass. `mail(subject, text)` sends, and is not called while the rules
    have mail off: the pass still reads, records and plans, and writes nothing
    into `mailed`, so what matches is mailed once mail is back. Returns the
    fields of its log line."""
    started = time.monotonic()
    now = time.time() if now is None else now
    summary = {"msg": "pass", "offers": 0, "met": 0, "history": 0, "filtered": 0, "mailed": 0, "feed": "error"}
    mailing = rules["mail"]
    if not mailing:
        summary["mail"] = "off"
    try:
        state = record.load_state()
        first = not state["up"]
        try:
            status, feed, last_modified = fetch_feed(state["last_modified"])
            offers = parse_offers(feed) if feed is not None else None
        except FeedError as e:
            summary.update(level="warn", error=str(e), feed="unreadable")
            state["failing_since"] = state["failing_since"] or now
            if mailing and now - state["failing_since"] >= ALERT_AFTER_S and not state["alerted"]:
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
            notices.append(up_notice([o for o in offers if allowed(o, rules["filters"])], rules["limits"]))
        to_mail = []
        if offers is None:
            record.touch(now)
        else:
            # Written before the mail: a mail that fails must not cost the history.
            record.observe(offers, now)
            cache = {}
            baseline = lambda kind: cache.setdefault(kind, record.baseline(kind, now))
            found = matches(offers, rules, baseline)
            summary["offers"] = len(offers)
            summary["filtered"] = len(matches(offers, rules, baseline, filtered=False)) - len(found)
            summary["met"] = sum(1 for o in offers if allowed(o, rules["filters"]) and any(meets(o, r) for r in rules["limits"]))
            summary["history"] = sum(1 for _, _, hit in found if hit)
            to_mail, planned = plan(found, offers, state["mailed"])
            if mailing:
                state["mailed"] = planned
            else:
                state["mailed"] = {i: p for i, p in state["mailed"].items() if i in planned}
                summary["would_mail"] = len(to_mail)
        message = build_mail(to_mail, notices, rules["vat_pct"]) if mailing else None
        if message:
            mail(*message)
        summary["mailed"] = len(to_mail) if mailing else 0
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

def coverage(record):
    """The first line of every view: how far back the record goes, and how much of it is the seed's."""
    first, last, total, seed = record.db.execute(
        "SELECT min(first_seen), max(last_seen), count(*), coalesce(sum(seed), 0) FROM offers").fetchone()
    if not total:
        return "the record is empty"
    days = (datetime.date.fromisoformat(last[:10]) - datetime.date.fromisoformat(first[:10])).days + 1
    return f"the record covers {days} days, {first[:10]} to {last[:10]}; {seed} of its {total} offers came from the seed"


def kind_rows(baselines, cheapest_live):
    """[(kind, baseline, offers in the window, cheapest live or None)], the kinds with the most offers first."""
    return [(kind, median, count, cheapest_live.get(kind))
            for kind, (median, count, _, _) in sorted(baselines.items(), key=lambda kv: (-kv[1][1], kv[0]))]


def show_kinds(record, now):
    live = dict(record.db.execute("SELECT kind, min(last_price) FROM offers WHERE gone=0 GROUP BY kind"))
    rows = kind_rows(record.baselines(now), live)
    print(coverage(record))
    print(f"{len(rows)} kinds with a baseline (the median last price of the offers of the kind that went in the last "
          f"{record.history['window_days']} days, from at least {record.history['min_offers']})")
    print("baseline_eur\toffers\tcheapest_now_eur\tkind")
    for kind, median, count, cheapest in rows:
        print(f"{median:g}\t{count}\t{'-' if cheapest is None else format(cheapest, 'g')}\t{kind}")


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


def kind_shape(kind):
    """(RAM in GB, has NVMe) from a kind's name: CPU|RAM|nvme|sata|hdd|gpu, read from the right."""
    _, ram, nvme, *_ = kind.rsplit("|", 5)
    return float(ram[:-2]), nvme != "nvme:"


def kind_parts(kind):
    """A kind's name taken apart: CPU, RAM, the total of each type's drives in GB, GPU."""
    cpu, ram, nvme, sata, hdd, gpu = kind.rsplit("|", 5)
    total = lambda drives_: sum(int(size) for size in drives_.partition(":")[2].split("+") if size)
    return {"cpu": cpu, "ram_gb": float(ram[:-2]), "nvme_gb": total(nvme), "sata_gb": total(sata),
            "hdd_gb": total(hdd), "gpu": gpu == "gpu"}


def kind_meets(kind, price, rule):
    """`meets`, for a kind's name and a price."""
    ram, nvme = kind_shape(kind)
    return ram >= rule["min_ram_gb"] and (not rule["nvme_required"] or nvme) and price < rule["max_price_eur"]


def group_names(rams):
    """{(RAM, has NVMe): name} for the groups of `--history --groups`."""
    return {(ram, nvme): f"{ram:g} GB {'with' if nvme else 'without'} NVMe" for ram in rams for nvme in (True, False)}


def period_of(stamp, by):
    """The month of a time, `2026-11`, or the Monday of its week."""
    if by == "month":
        return stamp[:7]
    day = datetime.date.fromisoformat(stamp[:10])
    return (day - datetime.timedelta(days=day.weekday())).isoformat()


def show_history(record, text, by, groups):
    """What offers went for, by period: the median of their last price, the lowest, and how many.
    By kind (those holding `text`, else every kind of a watched RAM) or by four groups."""
    rams = record.history["ram_gb"]
    if groups:
        names = group_names(rams)
        wanted = lambda kind: names.get(kind_shape(kind))
    else:
        wanted = lambda kind: kind if (text.casefold() in kind.casefold() if text else kind_shape(kind)[0] in rams) else None
    series = {name: {} for name in names.values()} if groups else {}
    for kind, price, seen in record.db.execute("SELECT kind, last_price, last_seen FROM offers WHERE gone=1"):
        if (name := wanted(kind)) is not None:
            series.setdefault(name, {}).setdefault(period_of(seen, by), []).append(price)
    print(coverage(record))
    if not any(series.values()):
        print("no offer went in a kind that matches")
        return 1
    ranked = list(series.items()) if groups else sorted(series.items(), key=lambda kv: (-sum(len(p) for p in kv[1].values()), kv[0]))
    shown = ranked if groups else ranked[:HISTORY_KINDS]
    what = "groups" if groups else "kinds"
    print(f"by {by}: the median and the lowest price the offers went for (their last price), from the offers that went; "
          f"{len(shown)} of {len(ranked)} {what}" + ("" if len(shown) == len(ranked) else f", the {len(shown)} with the most offers"))
    for name, periods in shown:
        print(f"\n{name}: {sum(len(p) for p in periods.values())} offers")
        print(f"{'month' if by == 'month' else 'week_from'}\tmedian_eur\tlowest_eur\toffers")
        for period in sorted(periods):
            prices = periods[period]
            print(f"{period}\t{statistics.median(prices):g}\t{min(prices):g}\t{len(prices)}")
    return 0


def span(seconds):
    return f"{int(seconds)}s" if seconds < QUICK_SECONDS else duration(seconds)


def nearest_rank(sorted_values, fraction):
    return sorted_values[math.ceil(fraction * len(sorted_values)) - 1]


def lifetime_stats(rows):
    """(went, half within, three quarters within, slowest, went in under two minutes, live) of a group's
    offers; the three times and the count under two minutes are None when none went."""
    went = sorted(r["life"] for r in rows if r["gone"])
    live = sum(1 for r in rows if not r["gone"])
    if not went:
        return 0, None, None, None, None, live
    return (len(went), nearest_rank(went, 0.5), nearest_rank(went, 0.75), went[-1],
            sum(1 for w in went if w < QUICK_SECONDS), live)


def lifetime_line(label, rows):
    count, half, three_quarters, slowest, quick, live = lifetime_stats(rows)
    if not count:
        return f"{label}\t0\t-\t-\t-\t-\t{live}"
    return f"{label}\t{count}\t{span(half)}\t{span(three_quarters)}\t{span(slowest)}\t{quick}\t{live}"


def epoch(stamp):
    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))


def lifetime_sections(rows, baselines, limits, text):
    """(the offers counted, [(title, [(group, its offers)])]) from (kind, first, last, last price, gone) rows
    of the offers this pod saw appear. A seed row has no sighting of its own (the seed was sampled
    every five minutes), and the offers of the pod's first read were there before it began: neither counts."""
    began = min((r[1] for r in rows), default=None)
    seen = [r for r in rows if r[1] != began and text.casefold() in r[0].casefold()]

    def under(kind, price):
        base = baselines.get(kind)
        return None if base is None else (1 - price / base[0]) * 100

    offers = [{"kind": k, "price": price, "gone": bool(gone), "life": epoch(last) - epoch(first), "under": under(k, price)}
              for k, first, last, price, gone in seen]
    bands = [("under 50 EUR", lambda o: o["price"] < 50), ("50 to 60 EUR", lambda o: 50 <= o["price"] < 60),
             ("60 to 100 EUR", lambda o: 60 <= o["price"] < 100), ("100 EUR or more", lambda o: o["price"] >= 100)]
    depth = [("20% or more under its usual price", lambda o: o["under"] is not None and o["under"] >= 20),
             ("10 to 20% under", lambda o: o["under"] is not None and 10 <= o["under"] < 20),
             ("under 10% under, or over", lambda o: o["under"] is not None and o["under"] < 10),
             ("no baseline for its kind", lambda o: o["under"] is None)]
    sections = [("by price band (the price it went for)", bands),
                ("by how far under its kind's usual price (the baseline of today)", depth),
                ("by the operator's limits (price and shape; the datacenter filters are not applied)",
                 [(rule["name"], lambda o, rule=rule: kind_meets(o["kind"], o["price"], rule)) for rule in limits])]
    return offers, [(title, [(label, [o for o in offers if test(o)]) for label, test in groups]) for title, groups in sections]


def show_lifetimes(record, limits, text, now):
    """How long offers stayed in the feed, from the rows this pod saw appear."""
    rows = record.db.execute(
        "SELECT kind, first_seen, last_seen, last_price, gone FROM offers"
        " WHERE seed=0 AND (pod_first_seen IS NULL OR first_seen=pod_first_seen)").fetchall()
    offers, sections = lifetime_sections(rows, record.baselines(now), limits, text)
    print(coverage(record))
    print(f"lifetimes of {len(offers)} offers this pod saw appear" + (f" in a kind holding '{text}'" if text else "")
          + "; the offers of its first read and the seed's rows are not counted")
    print(f"time in the feed, from first to last sighting: the feed changes once a minute, so a time under "
          f"{QUICK_SECONDS}s is one or two reads")
    header = "went\thalf_within\tthree_quarters_within\tslowest\tunder_2min\tstill_live"
    for title, groups in sections:
        print(f"\n{title}\n{'group'}\t{header}")
        for label, group in groups:
            print(lifetime_line(label, group))
    return 0


def parse_history_args(args):
    """(text, by, groups) from the words after --history, or None when they do not parse."""
    text, by, groups, rest = "", "month", False, list(args)
    while rest:
        word = rest.pop(0)
        if word == "--groups":
            groups = True
        elif word == "--by" and rest and rest[0] in ("month", "week"):
            by = rest.pop(0)
        elif not word.startswith("--") and not text:
            text = word
        else:
            return None
    return None if groups and text else (text, by, groups)


# --- Serving the record: read-only views as CSV -------------------------------

class ReadRecord(Record):
    """The record through a connection that cannot write: opened read-only and
    with `query_only`, so a bug in a view cannot touch what the watching writes."""

    def __init__(self, path, history):
        self.history = history
        self.db = sqlite3.connect(f"file:{urllib.parse.quote(path)}?mode=ro", uri=True, timeout=TIMEOUT,
                                  isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA query_only=ON")


def cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(round(value, 4))
    return str(value)


def csv_bytes(header, rows):
    """RFC 4180: CRLF line ends, a field quoted only when it has to be."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows([cell(v) for v in row] for row in rows)
    return out.getvalue().encode()


def median_or_none(prices):
    return statistics.median(prices) if prices else None


def days_between(first, last):
    """Every day, as YYYY-MM-DD, from one to the other."""
    start = datetime.date.fromisoformat(first)
    return [(start + datetime.timedelta(days=n)).isoformat()
            for n in range((datetime.date.fromisoformat(last) - start).days + 1)]


def live_at_end_of_day(spans, days):
    """{day: offers live at its end} from (first day, last day or None) spans: an offer is live at the
    end of every day from the one it appeared to the one before it went; one still in the feed never goes."""
    change = {}
    for first, last in spans:
        change[first] = change.get(first, 0) + 1
        if last is not None:
            change[last] = change.get(last, 0) - 1
    live, counts = 0, {}
    for day in days:
        live += change.get(day, 0)
        counts[day] = live
    return counts


class Snapshot:
    """What the views are computed from: the record as of one version of the feed, read in one
    transaction. The offers' bodies stay in the file: only the datacenter of the ones a view needs is read."""

    def __init__(self, record, rules, now):
        self.rules, self.history, self.now = rules, rules["history"], now
        record.db.execute("BEGIN")
        try:
            self.version = record._meta("last_modified") or ""
            self.rows = record.db.execute(
                "SELECT id, kind, ecc, first_seen, last_seen, last_price, gone, seed, pod_first_seen FROM offers").fetchall()
            self.datacenters = record.db.execute(
                "SELECT id, json_extract(offer, '$.Details.Datacenter.Name'), gone, last_price, last_seen FROM offers"
                " WHERE offer IS NOT NULL AND (gone=0 OR last_seen>=?)", (iso(now - 30 * 86400),)).fetchall()
            self.baselines = record.baselines(now)
        finally:
            record.db.execute("COMMIT")
        self.live = [r for r in self.rows if not r[6]]
        self.went = [r for r in self.rows if r[6]]
        self.days = days_between(min(r[3] for r in self.rows)[:10], max(r[4] for r in self.rows)[:10]) if self.rows else []


def view_summary(s, now):
    header = ["record_from", "record_to", "days", "offers", "offers_from_seed", "kinds", "live", "feed_read_at", "feed_age_seconds"]
    if not s.rows:
        return header, []
    last = max(r[4] for r in s.rows)
    read = max((r[4] for r in s.live), default=last)
    return header, [[min(r[3] for r in s.rows), last, len(s.days), len(s.rows), sum(r[7] for r in s.rows),
                     len({r[1] for r in s.rows}), len(s.live), read, max(0, round(now - epoch(read)))]]


def view_limits(s, now):
    header = ["name", "min_ram_gb", "nvme_required", "max_price_eur", "live_of_that_shape", "cheapest_live_eur",
              "live_under_limit", "met_last_7_days", "met_last_30_days"]
    rows = []
    for rule in s.rules["limits"]:
        shaped = [r[5] for r in s.live if kind_meets(r[1], 0, rule)]
        met = lambda days: sum(1 for r in s.went if r[4] >= iso(s.now - days * 86400) and kind_meets(r[1], r[5], rule))
        rows.append([rule["name"], rule["min_ram_gb"], rule["nvme_required"], rule["max_price_eur"], len(shaped), min(shaped, default=None),
                     sum(1 for p in shaped if p < rule["max_price_eur"]), met(7), met(30)])
    return header, rows


def view_groups_daily(s, now):
    header = ["day", "group", "offers", "median_eur", "lowest_eur", "live_at_end_of_day"]
    names = group_names(s.history["ram_gb"])
    prices, spans = {}, {name: [] for name in names.values()}
    for _, kind, _, first, last, price, gone, _, _ in s.rows:
        if (name := names.get(kind_shape(kind))) is not None:
            spans[name].append((first[:10], last[:10] if gone else None))
            if gone:
                prices.setdefault((last[:10], name), []).append(price)
    live = {name: live_at_end_of_day(group, s.days) for name, group in spans.items()}
    return header, [[day, name, len(prices.get((day, name), [])), median_or_none(prices.get((day, name), [])),
                     min(prices.get((day, name), []), default=None), live[name][day]]
                    for day in s.days for name in names.values()]


def view_flow_daily(s, now):
    header = ["day", "appeared", "gone", "live_at_end_of_day"]
    appeared, gone = {}, {}
    for r in s.rows:
        appeared[r[3][:10]] = appeared.get(r[3][:10], 0) + 1
        if r[6]:
            gone[r[4][:10]] = gone.get(r[4][:10], 0) + 1
    live = live_at_end_of_day([(r[3][:10], r[4][:10] if r[6] else None) for r in s.rows], s.days)
    return header, [[day, appeared.get(day, 0), gone.get(day, 0), live[day]] for day in s.days]


def view_limits_daily(s, now):
    header = ["day", "limit", "offers", "lowest_eur", "shape_offers", "lowest_of_shape_eur"]
    shaped, met = {}, {}
    for r in s.went:
        for rule in s.rules["limits"]:
            if kind_meets(r[1], 0, rule):
                shaped.setdefault((r[4][:10], rule["name"]), []).append(r[5])
                if r[5] < rule["max_price_eur"]:
                    met.setdefault((r[4][:10], rule["name"]), []).append(r[5])
    return header, [[day, rule["name"], len(met.get((day, rule["name"]), [])), min(met.get((day, rule["name"]), []), default=None),
                     len(shaped.get((day, rule["name"]), [])), min(shaped.get((day, rule["name"]), []), default=None)]
                    for day in s.days for rule in s.rules["limits"]]


def view_lifetimes(s, now):
    header = ["group", "offers", "half_within_seconds", "three_quarters_within_seconds", "slowest_seconds",
              "under_two_minutes", "live", "section"]
    rows = [(r[1], r[3], r[4], r[5], r[6]) for r in s.rows if not r[7] and (r[8] is None or r[3] == r[8])]
    _, sections = lifetime_sections(rows, s.baselines, s.rules["limits"], "")
    return header, [[label, *lifetime_stats(group), title] for title, groups in sections for label, group in groups]


def view_live(s, now):
    header = ["id", "cpu", "ram_gb", "nvme_gb", "sata_gb", "hdd_gb", "gpu", "ecc", "datacenter", "price_eur", "first_seen",
              "age_seconds", "baseline_eur", "under_baseline_pct", "limit", "url", "kind"]
    datacenter = {r[0]: r[1] for r in s.datacenters}
    rows = []
    for id_, kind, ecc, first, _, price, _, _, _ in sorted(s.live, key=lambda r: (r[5], r[0])):
        parts, base = kind_parts(kind), s.baselines.get(kind)
        rows.append([id_, parts["cpu"], parts["ram_gb"], parts["nvme_gb"], parts["sata_gb"], parts["hdd_gb"], parts["gpu"], bool(ecc),
                     datacenter.get(id_), price, first, max(0, round(now - epoch(first))), base[0] if base else None,
                     round((1 - price / base[0]) * 100, 1) if base else None,
                     "; ".join(rule["name"] for rule in s.rules["limits"] if kind_meets(kind, price, rule)),
                     f"{AUCTION_PAGE}?freetext={id_}", kind])
    return header, rows


def view_kinds(s, now):
    header = ["kind", "cpu", "ram_gb", "baseline_eur", "offers_in_window", "live", "cheapest_live_eur"]
    cheapest, live = {}, {}
    for r in s.live:
        live[r[1]] = live.get(r[1], 0) + 1
        cheapest[r[1]] = min(cheapest.get(r[1], r[5]), r[5])
    return header, [[kind, (parts := kind_parts(kind))["cpu"], parts["ram_gb"], median, count, live.get(kind, 0), cheapest_now]
                    for kind, median, count, cheapest_now in kind_rows(s.baselines, cheapest)]


def view_datacenters(s, now):
    header = ["datacenter", "live", "median_live_eur", "gone_last_30_days", "median_gone_last_30_days_eur"]
    live, gone = {}, {}
    for _, name, is_gone, price, _ in s.datacenters:
        if name:
            (gone if is_gone else live).setdefault(re.match(r"[A-Za-z]*", name).group(), []).append(price)
    return header, [[prefix, len(live.get(prefix, [])), median_or_none(live.get(prefix, [])), len(gone.get(prefix, [])),
                     median_or_none(gone.get(prefix, []))] for prefix in sorted(set(live) | set(gone))]


# What a path answers; `timed` views have a column that depends on the clock, so they are rendered at each
# request from the snapshot, which is what is computed once for a version of the feed.
VIEWS = {
    "/views/summary.csv": (view_summary, True),
    "/views/limits.csv": (view_limits, False),
    "/views/groups-daily.csv": (view_groups_daily, False),
    "/views/flow-daily.csv": (view_flow_daily, False),
    "/views/limits-daily.csv": (view_limits_daily, False),
    "/views/lifetimes.csv": (view_lifetimes, False),
    "/views/live.csv": (view_live, True),
    "/views/kinds.csv": (view_kinds, False),
    "/views/datacenters.csv": (view_datacenters, False),
}


class Views:
    """The views of the record, computed once for each version of the feed and answered from that
    until the next. A feed that sends no Last-Modified has no version: the views are then computed again after a minute."""

    def __init__(self, path, rules, clock=time.time):
        self.path, self.rules, self.clock = path, rules, clock
        self.lock = threading.Lock()
        self.record = None
        self.key = None
        self.snapshot = None
        self.bodies = {}
        self.computed = 0

    def _open(self):
        if self.record is None:
            self.record = ReadRecord(self.path, self.rules["history"])
        return self.record

    def healthy(self):
        with self.lock:
            try:
                self._open().db.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
                return True
            except sqlite3.Error:
                self.record = None
                return False

    def get(self, path):
        """(body, version's date as an HTTP date), or None for a path that is not a view. Raises sqlite3.Error."""
        if path not in VIEWS:
            return None
        function, timed = VIEWS[path]
        with self.lock:
            try:
                now = self.clock()
                version = self._open()._meta("last_modified") or ""
                key = version or int(now // 60)
                if key != self.key or self.snapshot is None:
                    self.snapshot, self.key, self.bodies = Snapshot(self.record, self.rules, now), key, {}
                    self.computed += 1
                snapshot = self.snapshot
                if timed:
                    body = csv_bytes(*function(snapshot, now))
                else:
                    if path not in self.bodies:
                        self.bodies[path] = csv_bytes(*function(snapshot, now))
                    body = self.bodies[path]
            except sqlite3.Error:
                self.record = None
                raise
        return body, version or email.utils.formatdate(snapshot.now, usegmt=True)


class ViewHandler(http.server.BaseHTTPRequestHandler):
    server_version = "auction-watch"
    sys_version = ""
    timeout = REQUEST_TIMEOUT

    def log_message(self, *_):
        pass

    def _send(self, status, body=b"", content_type="text/plain; charset=utf-8", head=False, **headers):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _answer(self, head):
        path = self.path.partition("?")[0]
        views = self.server.views
        try:
            if path == "/healthz":
                ok = views.healthy()
                return self._send(200 if ok else 503, b"ok\n" if ok else b"the record cannot be read\n", head=head, Cache_Control="no-store")
            found = views.get(path)
        except Exception as e:                        # a request never ends anything but itself
            log(level="error", msg="view failed", error=type(e).__name__, path=path[:80])
            return self._send(500, b"error\n", head=head)
        if found is None:
            return self._send(404, b"not found\n", head=head)
        body, modified = found
        self._send(200, body, "text/csv; charset=utf-8", head, Last_Modified=modified, Cache_Control=f"max-age={VIEW_CACHE_SECONDS}")

    def do_GET(self):
        self._answer(head=False)

    def do_HEAD(self):
        self._answer(head=True)

    def _refuse(self):
        self._send(405, b"only GET and HEAD\n", Allow="GET, HEAD")

    def __getattr__(self, name):
        if name.startswith("do_"):                     # any other method, known or not
            return self._refuse
        raise AttributeError(name)


class ViewServer(http.server.ThreadingHTTPServer):
    """One thread for each request, at most MAX_CONNECTIONS at once: another is closed with a 503, and
    one that stalls is dropped after REQUEST_TIMEOUT seconds."""
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, views):
        super().__init__(address, ViewHandler)
        self.views = views
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            with contextlib.suppress(OSError):
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            return self.shutdown_request(request)
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        log(level="warn", msg="request failed", error=type(sys.exc_info()[1]).__name__)


def serve(path, rules, host="", port=SERVE_PORT):
    """Starts the views in a thread of this process and returns the server. The thread is a daemon and
    reads through a connection of its own: nothing it does reaches the loop. A port that cannot be bound is
    logged, and the watching goes on without views."""
    try:
        server = ViewServer((host, port), Views(path, rules))
    except OSError as e:
        log(level="error", msg="views not served", error=type(e).__name__)
        return None
    threading.Thread(target=server.serve_forever, name="views", daemon=True).start()
    log(msg="views served", port=server.server_address[1])
    return server


# --- Self-test and dry run ---------------------------------------------------

def _offer(id_, ram, nvme, price, cpu="CPU", gpu=False):
    return {"id": id_, "cpu": cpu, "ram_gb": ram, "ecc": False, "nvme": nvme, "sata": [], "hdd": [], "gpu": gpu,
            "price": price, "ip_price": 1.7, "datacenter": "FSN1-DC1", "specials": [], "next_drop_s": 3600, "raw": {"Id": id_}}


def _seed_file(path, rows):
    with gzip.open(path, "wt") as f:
        for id_, kind, first, last, low, price in rows:
            f.write(json.dumps({"id": id_, "kind": kind, "ecc": False, "first": first, "last": last,
                                "first_price": price, "low": low, "price": price}) + "\n")


def _lines(function, *args):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        status = function(*args)
    return status, out.getvalue().splitlines()


def self_test_views(tmp, history, limits):
    """--history and --lifetimes, on a record of known months, weeks and lifetimes."""
    now, day = 1_800_000_000, 86400
    k64, k64n, k128, k32 = (kind_of(_offer(0, ram, nvme, 0)) for ram, nvme in ((64, [512]), (64, []), (128, [512]), (32, [512])))
    assert kind_shape(k64) == (64, True) and kind_shape(k64n) == (64, False) and kind_shape(k128) == (128, True)
    assert kind_shape(kind_of(_offer(0, 64, [512], 0, cpu="A|B"))) == (64, True)
    assert period_of("2026-11-23T10:00:00Z", "month") == "2026-11" and period_of("2026-11-23T10:00:00Z", "week") == "2026-11-23"
    assert period_of("2026-11-29T23:59:59Z", "week") == "2026-11-23" and period_of("2026-11-30T00:00:00Z", "week") == "2026-11-30"
    seed = os.path.join(tmp, "views-seed.jsonl.gz")
    rows = [(1, k64, "2026-09-01T00:00:00Z", "2026-09-10T00:00:00Z", 40, 50), (2, k64, "2026-09-01T00:00:00Z", "2026-09-20T00:00:00Z", 60, 70),
            (3, k64, "2026-09-01T00:00:00Z", "2026-10-02T00:00:00Z", 80, 90), (4, k64n, "2026-09-01T00:00:00Z", "2026-10-03T00:00:00Z", 30, 30),
            (5, k128, "2026-09-01T00:00:00Z", "2026-10-03T00:00:00Z", 90, 99), (6, k32, "2026-09-01T00:00:00Z", "2026-10-03T00:00:00Z", 10, 10)]
    _seed_file(seed, rows)
    store = Record(os.path.join(tmp, "views.db"), history, seed)

    status, lines = _lines(show_history, store, "", "month", False)
    assert status == 0 and lines[0] == "the record covers 33 days, 2026-09-01 to 2026-10-03; 6 of its 6 offers came from the seed", lines[0]
    assert "3 of 3 kinds" in lines[1]                                               # the 32 GB kind's RAM is not watched
    assert lines[3] == f"{k64}: 3 offers" and lines[5] == "2026-09\t60\t50\t2" and lines[6] == "2026-10\t90\t90\t1", lines
    assert all(k32 not in line for line in lines)
    _, lines = _lines(show_history, store, "", "week", False)
    assert "week_from\tmedian_eur\tlowest_eur\toffers" in lines and "2026-09-07\t50\t50\t1" in lines and "2026-09-14\t70\t70\t1" in lines
    assert "2026-09-28\t90\t90\t1" in lines                                      # 2026-10-02 is in the week of 2026-09-28
    _, lines = _lines(show_history, store, "nvme:512", "month", False)             # TEXT holds any RAM, the 32 GB kind too
    assert any(k32 in line for line in lines) and all(k64n not in line for line in lines)
    assert _lines(show_history, store, "no such kind", "month", False)[0] == 1
    _, lines = _lines(show_history, store, "", "month", True)
    assert "4 of 4 groups" in lines[1] and "64 GB with NVMe: 3 offers" in lines and "64 GB without NVMe: 1 offers" in lines
    assert "128 GB with NVMe: 1 offers" in lines and "128 GB without NVMe: 0 offers" in lines and all("32 GB" not in line for line in lines)
    assert "2026-09\t60\t50\t2" in lines
    assert parse_history_args([]) == ("", "month", False) and parse_history_args(["X", "--by", "week"]) == ("X", "week", False)
    assert parse_history_args(["--groups", "--by", "month"]) == ("", "month", True)
    assert parse_history_args(["--by", "year"]) is None and parse_history_args(["--groups", "X"]) is None
    assert parse_history_args(["A", "B"]) is None and parse_history_args(["--nope"]) is None
    many = Record(os.path.join(tmp, "many.db"), history)
    for n in range(HISTORY_KINDS + 3):
        many.observe([_offer(n, 64, [512], 50, cpu=f"CPU{n:02d}")], now + n * 60 - 1)
    many.observe([], now + 3600)
    _, lines = _lines(show_history, many, "", "month", False)
    assert f"{HISTORY_KINDS} of {HISTORY_KINDS + 3} kinds, the {HISTORY_KINDS} with the most offers" in lines[1]
    assert sum(1 for line in lines if line.endswith(": 1 offers")) == HISTORY_KINDS
    assert _lines(show_history, Record(os.path.join(tmp, "empty.db"), history), "", "month", False) == (1, ["the record is empty", "no offer went in a kind that matches"])

    # Lifetimes: 900 is of the first read (left out); 901 stays two minutes, 902 one read, 903 is live.
    life = Record(os.path.join(tmp, "life.db"), history, seed)
    o900, o901, o902, o903 = _offer(900, 64, [512], 99), _offer(901, 64, [512], 45), _offer(902, 64, [512], 55), _offer(903, 64, [512], 70)
    life.observe([o900], now)
    life.observe([o900, o901, o902, o903], now + 60)
    life.observe([o900, o901, o903], now + 120)
    life.touch(now + 180)
    life.observe([o900, o903], now + 240)
    status, lines = _lines(show_lifetimes, life, limits, "", now + 300)
    assert status == 0 and lines[0].startswith("the record covers ") and "6 of its 10 offers came from the seed" in lines[0], lines[0]
    assert "lifetimes of 3 offers" in lines[1] and "the feed changes once a minute" in lines[2]
    column = lambda label: next(line.split("\t")[1:] for line in lines if line.startswith(label + "\t"))
    assert column("under 50 EUR") == ["1", "2m", "2m", "2m", "0", "0"]
    assert column("50 to 60 EUR") == ["1", "0s", "0s", "0s", "1", "0"]
    assert column("60 to 100 EUR") == ["0", "-", "-", "-", "-", "1"]
    assert column("100 EUR or more") == ["0", "-", "-", "-", "-", "0"]
    assert column(limits[0]["name"]) == ["2", "0s", "2m", "2m", "1", "0"]
    assert column(limits[1]["name"]) == ["0", "-", "-", "-", "-", "0"]
    assert column("no baseline for its kind") == ["2", "0s", "2m", "2m", "1", "1"]
    # A kind with a baseline: its offers are measured against it.
    base_kind = kind_of(_offer(0, 64, [512], 0))
    _seed_file(seed, [(2000 + i, base_kind, iso(now - 50 * day), iso(now - 40 * day), 100, 100) for i in range(10)])
    based = Record(os.path.join(tmp, "based.db"), history, seed)
    based.observe([o900], now)
    based.observe([o900, o901, o902], now + 60)
    based.observe([o900], now + 120)
    _, lines = _lines(show_lifetimes, based, limits, "", now + 300)
    assert column("20% or more under its usual price") == ["2", "0s", "0s", "0s", "2", "0"]      # 45 and 55 against 100
    assert column("no baseline for its kind") == ["0", "-", "-", "-", "-", "0"]
    _, lines = _lines(show_lifetimes, based, limits, "no such kind", now + 300)
    assert "lifetimes of 0 offers" in lines[1] and column("under 50 EUR") == ["0", "-", "-", "-", "-", "0"]
    assert nearest_rank([1, 2, 3, 4], 0.5) == 2 and nearest_rank([1, 2, 3, 4], 0.75) == 3 and nearest_rank([7], 0.75) == 7
    assert span(119) == "119s" and span(120) == "2m" and span(3700) == "1h 1m"
    for store_ in (store, many, life, based):
        store_.db.close()


def _offer_at(id_, ram, nvme, price, datacenter):
    offer = _offer(id_, ram, nvme, price)
    offer["datacenter"] = datacenter
    offer["raw"] = {"Id": id_, "Details": {"Datacenter": {"Name": datacenter}}}
    return offer


def self_test_serving(tmp, rules):
    """The views, over HTTP on a loopback port, against a record of known offers, and what they refuse."""
    now, day = 1_800_000_000, 86400                   # 2027-01-15T08:00:00Z
    history = rules["history"]
    k64, k64n, k128 = (kind_of(_offer(0, ram, nvme, 0)) for ram, nvme in ((64, [512]), (64, []), (128, [512])))
    seed = os.path.join(tmp, "serving-seed.jsonl.gz")
    _seed_file(seed, [(1, k64, iso(now - 3 * day), iso(now - 3 * day + 3600), 50, 50), (2, k64, iso(now - 3 * day), iso(now - 2 * day), 70, 70),
                      (3, k64n, iso(now - 40 * day), iso(now - 40 * day + 60), 30, 30), (4, k128, iso(now - 10 * day), iso(now - 9 * day), 90, 90)])
    path = os.path.join(tmp, "serving.db")
    record = Record(path, history, seed)
    a, b, c = _offer_at(100, 64, [512], 55, "FSN1-DC1"), _offer_at(101, 128, [512], 95, "HEL1-DC2"), _offer_at(102, 64, [512], 65, "NBG1-DC3")
    stamp = lambda minute: {"mailed": {}, "failing_since": 0, "alerted": False, "up": True,
                            "last_modified": f"Fri, 15 Jan 2027 08:0{minute}:00 GMT"}
    record.observe([a, b], now)
    record.observe([a, b, c], now + 60)
    record.observe([dict(a, price=52), c], now + 120)       # 101 went at 95
    record.save_state(stamp(2))
    clock = {"now": now + 300}
    handler_timeout = ViewHandler.timeout
    ViewHandler.timeout = 1
    server = serve(path, rules, "127.0.0.1", 0)
    views = server.views
    views.clock = lambda: clock["now"]
    port = server.server_address[1]
    assert serve(path, rules, "127.0.0.1", port) is None                # a port taken: logged, nothing raised

    def ask(method, target, port=port):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(method, target)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body

    def table(target):
        response, body = ask("GET", target)
        assert response.status == 200, (target, response.status)
        assert response.getheader("Content-Type") == "text/csv; charset=utf-8" and response.getheader("Cache-Control") == "max-age=60"
        assert body.endswith(b"\r\n") and b"\n" not in body.replace(b"\r\n", b"")
        return list(csv.reader(io.StringIO(body.decode())))

    try:
        # Each view: its header, and a known row.
        summary = table("/views/summary.csv")
        assert summary[0] == ["record_from", "record_to", "days", "offers", "offers_from_seed", "kinds", "live", "feed_read_at", "feed_age_seconds"]
        assert summary[1:] == [["2026-12-06T08:00:00Z", "2027-01-15T08:02:00Z", "41", "7", "4", "3", "2", "2027-01-15T08:02:00Z", "180"]], summary
        limits = table("/views/limits.csv")
        assert limits[0] == ["name", "min_ram_gb", "nvme_required", "max_price_eur", "live_of_that_shape", "cheapest_live_eur",
                             "live_under_limit", "met_last_7_days", "met_last_30_days"]
        assert limits[1:] == [[rules["limits"][0]["name"], "64", "true", "60", "2", "52", "1", "1", "1"],
                              [rules["limits"][1]["name"], "128", "true", "100", "0", "", "0", "1", "2"]], limits
        groups = table("/views/groups-daily.csv")
        assert groups[0] == ["day", "group", "offers", "median_eur", "lowest_eur", "live_at_end_of_day"] and len(groups) == 1 + 41 * 4
        assert ["2026-12-06", "64 GB without NVMe", "1", "30", "30", "0"] in groups
        assert ["2027-01-06", "128 GB with NVMe", "1", "90", "90", "0"] in groups and ["2027-01-05", "128 GB with NVMe", "0", "", "", "1"] in groups
        assert ["2027-01-15", "64 GB with NVMe", "0", "", "", "2"] in groups and ["2027-01-15", "128 GB with NVMe", "1", "95", "95", "0"] in groups
        flow = table("/views/flow-daily.csv")
        assert flow[0] == ["day", "appeared", "gone", "live_at_end_of_day"] and len(flow) == 1 + 41
        assert ["2027-01-12", "2", "1", "1"] in flow and ["2027-01-15", "3", "1", "2"] in flow
        per_limit = table("/views/limits-daily.csv")
        assert per_limit[0] == ["day", "limit", "offers", "lowest_eur", "shape_offers", "lowest_of_shape_eur"] and len(per_limit) == 1 + 41 * 2
        assert ["2027-01-12", rules["limits"][0]["name"], "1", "50", "1", "50"] in per_limit
        assert ["2027-01-13", rules["limits"][0]["name"], "0", "", "1", "70"] in per_limit
        lifetimes = table("/views/lifetimes.csv")
        assert lifetimes[0] == ["group", "offers", "half_within_seconds", "three_quarters_within_seconds", "slowest_seconds",
                                "under_two_minutes", "live", "section"] and len(lifetimes) == 1 + 4 + 4 + 2
        assert lifetimes[3][:7] == ["60 to 100 EUR", "0", "", "", "", "", "1"]
        live = table("/views/live.csv")
        assert live[0] == ["id", "cpu", "ram_gb", "nvme_gb", "sata_gb", "hdd_gb", "gpu", "ecc", "datacenter", "price_eur", "first_seen",
                           "age_seconds", "baseline_eur", "under_baseline_pct", "limit", "url", "kind"]
        assert live[1:] == [["100", "CPU", "64", "512", "0", "0", "false", "false", "FSN1-DC1", "52", "2027-01-15T08:00:00Z", "300", "", "",
                             rules["limits"][0]["name"], AUCTION_PAGE + "?freetext=100", k64],
                            ["102", "CPU", "64", "512", "0", "0", "false", "false", "NBG1-DC3", "65", "2027-01-15T08:01:00Z", "240", "", "",
                             "", AUCTION_PAGE + "?freetext=102", k64]], live
        assert table("/views/kinds.csv") == [["kind", "cpu", "ram_gb", "baseline_eur", "offers_in_window", "live", "cheapest_live_eur"]]
        assert table("/views/datacenters.csv") == [
            ["datacenter", "live", "median_live_eur", "gone_last_30_days", "median_gone_last_30_days_eur"],
            ["FSN", "1", "52", "0", ""], ["HEL", "0", "", "1", "95"], ["NBG", "1", "65", "0", ""]]
        response, body = ask("GET", "/views/live.csv")
        assert response.getheader("Last-Modified") == "Fri, 15 Jan 2027 08:02:00 GMT"
        response, body = ask("HEAD", "/views/live.csv")
        assert response.status == 200 and body == b"" and int(response.getheader("Content-Length")) > 100
        assert ask("GET", "/views/live.csv?x=1")[0].status == 200
        health, body = ask("GET", "/healthz")
        assert health.status == 200 and body == b"ok\n"

        # A baseline: ten offers of a kind went in the window, and the live offer is measured against it.
        usual = Record(os.path.join(tmp, "usual.db"), history)
        usual.observe([_offer_at(500 + n, 64, [512], 100, "FSN1-DC1") for n in range(10)], now - 10 * day)
        usual.observe([_offer_at(600, 64, [512], 70, "FSN1-DC1")], now - 9 * day)
        usual.save_state(stamp(1))
        with_base = Views(os.path.join(tmp, "usual.db"), rules, clock=lambda: now)
        assert with_base.get("/views/kinds.csv")[0] == csv_bytes(
            ["kind", "cpu", "ram_gb", "baseline_eur", "offers_in_window", "live", "cheapest_live_eur"], [[k64, "CPU", 64.0, 100.0, 10, 1, 70.0]])
        assert b"600,CPU,64,512,0,0,false,false,FSN1-DC1,70," in with_base.get("/views/live.csv")[0]
        assert b",100,30," in with_base.get("/views/live.csv")[0]                      # 30% under the usual 100

        # Read-only and fixed: only GET and HEAD, no other path, and no write through the views' connection.
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "FOO"):
            response, _ = ask(method, "/views/live.csv")
            assert response.status == 405 and response.getheader("Allow") == "GET, HEAD", method
        for target in ("/", "/views", "/views/", "/views/other.csv", "/views/live.csv/", "/views/../etc/passwd", "/healthz/x",
                       "/views/live.csv'%20OR%201=1", "/%2e%2e/%2e%2e/etc/passwd", "/VIEWS/LIVE.CSV"):
            assert ask("GET", target)[0].status == 404, target
        try:
            views.record.db.execute("UPDATE offers SET gone=1")
            raise AssertionError("wrote")
        except sqlite3.OperationalError:
            pass
        assert record.db.execute("SELECT count(*) FROM offers WHERE gone=0").fetchone() == (2,)

        # Computed once for a version of the feed, and again for the next; the clock moves only the ages.
        assert views.computed == 1
        clock["now"] += 30
        assert table("/views/live.csv")[1][11] == "330" and views.computed == 1
        record.observe([dict(a, price=51), c], now + 360)
        assert table("/views/live.csv")[1][9] == "52" and views.computed == 1       # the same version: the same views
        record.save_state(stamp(6))
        assert table("/views/live.csv")[1][9] == "51" and views.computed == 2

        # Serving never touches the watching: a stalled client, one too many, and a failing view.
        stalled = []
        for _ in range(MAX_CONNECTIONS):
            stalled.append(socket.create_connection(("127.0.0.1", port), timeout=5))
            stalled[-1].sendall(b"GET /views/live.csv HTTP/1.1\r\nHost: x\r\n")      # never finished
        extra = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert extra.recv(100).startswith(b"HTTP/1.0 503")
        extra.close()
        started = time.monotonic()
        record.observe([c], now + 420)                                               # the loop's write, while all are held
        assert time.monotonic() - started < 1
        assert stalled[0].recv(100) == b""                                           # the stalled client is dropped
        for held in stalled:
            held.close()
        assert ask("GET", "/healthz")[0].status == 200
        original = VIEWS["/views/kinds.csv"]
        VIEWS["/views/kinds.csv"] = (lambda s, n: 1 / 0, False)
        try:
            assert ask("GET", "/views/kinds.csv")[0].status == 500
        finally:
            VIEWS["/views/kinds.csv"] = original
        assert ask("GET", "/views/kinds.csv")[0].status == 200

        # An empty record: the header alone.
        empty = Views(os.path.join(tmp, "serving-empty.db"), rules, clock=lambda: now)
        Record(os.path.join(tmp, "serving-empty.db"), history).db.close()
        for target, (function, _) in VIEWS.items():
            rows = list(csv.reader(io.StringIO(empty.get(target)[0].decode())))
            assert (len(rows) == 1) == (target not in ("/views/limits.csv", "/views/lifetimes.csv")), target
        assert empty.get("/views/limits.csv")[1].endswith("GMT") and empty.healthy()
        missing = Views(os.path.join(tmp, "no-such.db"), rules)
        assert not missing.healthy()
        try:
            missing.get("/views/live.csv")
            raise AssertionError("served")
        except sqlite3.Error:
            pass
    finally:
        server.shutdown()
        server.server_close()
        ViewHandler.timeout = handler_timeout
        for store_ in (record, usual):
            store_.db.close()


def self_test():
    global fetch_feed
    rules = parse_rules(json.load(open(RULES_FILE)))
    rules["mail"] = True                       # the passes below mail; the shipped rules may have it off
    r64, r128 = rules["limits"]
    history = rules["history"]
    assert len(rules["limits"]) == 2
    assert (r64["min_ram_gb"], r64["nvme_required"], r64["max_price_eur"]) == (64, True, 60)
    assert (r128["min_ram_gb"], r128["nvme_required"], r128["max_price_eur"]) == (128, True, 100)
    assert history == {"ram_gb": [64.0, 128.0], "under_pct": 20.0, "window_days": 90, "min_offers": 10}
    assert rules["vat_pct"] == 21
    for bad_vat in (-1, 100, "x"):
        try:
            parse_rules({**json.load(open(RULES_FILE)), "vat_pct": bad_vat})
            raise AssertionError("accepted")
        except ValueError:
            pass
    vat = rules["vat_pct"]
    filters = rules["filters"]
    assert filters == {"datacenter_prefixes": ["FSN", "NBG"], "skip_hdd_only": True}

    # The filters: Germany only, and never HDD alone; any other drive makes it mailable.
    def at(dc, nvme=(), sata=(), hdd=()):
        o = _offer(1, 64, list(nvme), 10)
        o.update(datacenter=dc, sata=list(sata), hdd=list(hdd))
        return o
    assert allowed(at("FSN1-DC14", [512]), filters) and allowed(at("NBG1-DC3", [512]), filters)
    assert not allowed(at("HEL1-DC2", [512]), filters)
    assert not allowed(at("FSN1-DC14", hdd=[4000]), filters)
    assert allowed(at("FSN1-DC14", sata=[512], hdd=[4000]), filters) and allowed(at("FSN1-DC14", nvme=[512], hdd=[4000]), filters)
    assert allowed(at("FSN1-DC14"), filters)                       # no drive listed is not "only HDD"
    cheap_hel, cheap_hdd = at("HEL1-DC2", [512]), at("FSN1-DC14", hdd=[4000])
    wide = {**rules, "limits": [{**r64, "nvme_required": False}]}
    assert [o["datacenter"] for o, _, _ in matches([cheap_hel, cheap_hdd, at("NBG1-DC3", [512])], wide, lambda k: None)] == ["NBG1-DC3"]
    assert len(matches([cheap_hel, cheap_hdd], wide, lambda k: None, filtered=False)) == 2
    # And the history rule: far under the usual, yet never mailed from Helsinki.
    usual_base = lambda k: (100.0, 12, "2026-09-01", "2026-11-20")
    assert matches([cheap_hel], {**rules, "limits": []}, usual_base) == []
    assert len(matches([cheap_hel], {**rules, "limits": []}, usual_base, filtered=False)) == 1
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
        # A seed offer the feed shows stops being a seed row: it takes the body and specials,
        # keeps the seed's first_seen, and records when this pod first saw it.
        seen = lambda i: record.db.execute(
            "SELECT first_seen, pod_first_seen, seed, specials, offer FROM offers WHERE id=?", (i,)).fetchone()
        assert seen(300) == (iso(now - 50 * day), None, 1, None, None)
        record.observe([_offer(300, 64, [1000], 80)], now + 10)
        assert seen(300) == (iso(now - 50 * day), iso(now + 10), 0, "[]", '{"Id":300}')
        record.observe([_offer(300, 64, [1000], 79)], now + 20)
        assert seen(300)[1] == iso(now + 10)
        record.db.execute("DELETE FROM offers WHERE id=300")
        record.db.commit()
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
    text = build_mail(found[:1], [], vat)[1]
    for needle in ("Offer 1", "55 EUR a month without VAT", "Prices are Prices.monthly.EUR, without VAT, and so are the limits",
                   "With 21% VAT: 66.55 EUR", "With the IPv4 address (1.7 EUR without VAT): 56.7 EUR without VAT, 68.61 EUR with VAT",
                   r64["name"], pct_name, "35% under the usual 84.5 EUR", "10 such offers",
                   "2026-09-01 to 2026-11-20", "1h", "64 GB", "not ECC", "1 x NVME 512 GB", "FSN1-DC1", AUCTION_PAGE + "?freetext=1"):
        assert needle in text, needle
    assert "History:" not in build_mail(found[2:], [], vat)[1]
    no_ip = _offer(7, 64, [512], 55)
    no_ip["ip_price"] = None
    assert "IPv4 address: its price is not in the feed" in build_mail([(no_ip, ["rule"], None)], [], vat)[1]
    ip_feed = {"IPPrices": {"monthly": {"EUR": 1.7}}}
    assert _ip_price(ip_feed) == 1.7 and _ip_price({}) is None and _ip_price({"IPPrices": {"monthly": {"EUR": "x"}}}) is None
    mixed = _offer(8, 64, [512], 55)
    mixed["sata"], mixed["hdd"] = [512, 1000, 512], [4000, 4000]
    assert drives(mixed) == "1 x NVME 512 GB, 2 x SATA SSD 512 GB, 1 x SATA SSD 1000 GB, 2 x HDD 4000 GB"
    assert drives(_offer(9, 64, [], 55)) == "none listed"
    assert build_mail([], [], vat) is None
    subject, text = build_mail(found, [], vat)
    assert subject == "auction-watch: CPU, 64 GB RAM, 55 EUR without VAT and 2 more" and "offer" not in subject
    assert text.startswith("CPU, 64 GB RAM, 55 EUR without VAT and 2 more. ")
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
                        "Prices": {"monthly": {"EUR": 55}}, "Details": {"Datacenter": {"Name": "FSN1-DC1"}}}]}
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
        assert result["mailed"] == 1 and len(sent) == 1 and "is up" in sent[0] and "c, 64 GB RAM, 55 EUR" in sent[0]
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
        assert result["history"] == 1 and result["met"] == 0 and result["mailed"] == 1 and sent[0] == "auction-watch: c, 64 GB RAM, 70 EUR without VAT"
        run_pass(store2, rules, mail, now + 60)
        assert len(sent) == 1
        good["server"][0]["Prices"]["monthly"]["EUR"] = 69
        run_pass(store2, rules, mail, now + 120)
        assert len(sent) == 2

        # An offer in Helsinki is recorded and counted as left out, never mailed.
        good["server"][0]["Prices"]["monthly"]["EUR"] = 55
        good["server"][0]["Details"]["Datacenter"]["Name"] = "HEL1-DC2"
        store4 = Record(os.path.join(tmp, "record4.db"), history)
        sent.clear()
        result = run_pass(store4, rules, mail, now)
        assert result["mailed"] == 0 and result["filtered"] == 1 and result["met"] == 0 and result["offers"] == 1
        assert sent == ["auction-watch is up: the feed holds 0 offers"]      # the first mail follows the filters too
        assert store4.db.execute("SELECT count(*) FROM offers").fetchone() == (1,)
        good["server"][0]["Details"]["Datacenter"]["Name"] = "FSN1-DC1"

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

        # Mail off: the pass records and plans, calls nothing, and writes nothing into `mailed`.
        silent = dict(rules, mail=False)
        store5 = Record(os.path.join(tmp, "record5.db"), history)
        sent.clear()
        result = run_pass(store5, silent, mail, now)
        assert sent == [] and result["mail"] == "off" and result["mailed"] == 0 and result["met"] == 1 and result["would_mail"] == 1
        assert store5.load_state()["mailed"] == {} and store5.db.execute("SELECT count(*) FROM offers").fetchone() == (1,)
        feed["answer"] = ("error", None, "")
        for n in range(0, 15 * 6):
            run_pass(store5, silent, refusing, now + 60 + n * 10)
        run_pass(store5, silent, refusing, now + 3000)           # no alert either, and nothing marked alerted
        assert store5.load_state()["alerted"] is False
        feed["answer"] = ("200", good, "t2")
        result = run_pass(store5, rules, mail, now + 3060)       # mail back on: the offer that still matches goes out
        assert result["mailed"] == 1 and "mail" not in result and len(sent) == 1 and store5.load_state()["mailed"] == {"1": 55}
        try:
            parse_rules({**json.load(open(RULES_FILE)), "mail": "no"})
            raise AssertionError("accepted")
        except ValueError:
            pass
        assert parse_rules(dict(json.load(open(RULES_FILE)), mail=False))["mail"] is False
        no_key = json.load(open(RULES_FILE))
        no_key.pop("mail", None)
        assert parse_rules(no_key)["mail"] is True

        # The looks.
        snap = os.path.join(tmp, "snap.db")
        store2.snapshot(snap)
        assert sqlite3.connect(snap).execute("SELECT count(*) FROM offers").fetchone() == (11,)
        with contextlib.redirect_stdout(io.StringIO()) as shown:
            show_kinds(store2, now + 3600)
            assert show_offer(store2, 1, now) == 0 and show_offer(store2, 100, now) == 0 and show_offer(store2, 5, now) == 1
        assert "90\t10\t69\t" in shown.getvalue() and "not seen by this record (seed)" in shown.getvalue()
        assert "pod_first_seen: " in shown.getvalue() and "first_seen: " in shown.getvalue()
        assert shown.getvalue().startswith("the record covers ")

        self_test_views(tmp, history, rules["limits"])
        self_test_serving(tmp, rules)

        # A record written before the fix: its seed rows the pod saw are put right at the next start, once.
        old = os.path.join(tmp, "old.db")
        legacy = Record(old, history, seed)
        legacy.db.execute("ALTER TABLE offers DROP COLUMN pod_first_seen")
        legacy.db.execute("DELETE FROM meta WHERE key='seed_rows_repaired'")
        legacy.db.execute("UPDATE offers SET last_seen='2020-01-01T00:00:00Z'")      # the seed's, before the first start
        legacy.db.execute("UPDATE offers SET last_seen=?, gone=0 WHERE id=100", (iso(time.time() + 60),))
        legacy.db.commit()
        legacy.db.close()
        fixed = Record(old, history, seed)
        pair = lambda i: fixed.db.execute("SELECT seed, pod_first_seen IS NOT NULL FROM offers WHERE id=?", (i,)).fetchone()
        assert pair(100) == (0, 1) and pair(101) == (1, 0)
        fixed.db.execute("UPDATE offers SET seed=1 WHERE id=100")
        fixed.db.commit()
        fixed.db.close()
        assert Record(old, history, seed).db.execute("SELECT seed FROM offers WHERE id=100").fetchone() == (1,)    # not again
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
    if not rules["mail"]:
        print("mail is off in these rules: the pod would send nothing; this is what it would have mailed", file=sys.stderr)
    print(f"{len(offers)} offers: {sum(1 for o in offers if any(meets(o, r) for r in rules['limits']))} meet a limit, "
          f"{sum(1 for _, _, h in found if h)} are far under their usual price, {len(to_mail)} would be mailed", file=sys.stderr)
    notices = [up_notice([o for o in offers if allowed(o, rules["filters"])], rules["limits"])] if first_pass else []
    message = build_mail(to_mail, notices, rules["vat_pct"])
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
    if args[:1] == ["--history"] and (parsed := parse_history_args(args[1:])):
        return show_history(Record(DB_FILE, rules["history"]), *parsed)
    if args[:1] == ["--lifetimes"] and len(args) <= 2 and not (args[1:] and args[1].startswith("--")):
        return show_lifetimes(Record(DB_FILE, rules["history"]), rules["limits"], args[1] if args[1:] else "", time.time())
    if args[:1] == ["--snapshot"] and len(args) == 2:
        Record(DB_FILE, rules["history"]).snapshot(args[1])
        return 0
    if args:
        sys.exit(__doc__)
    api_key, to = os.environ["RESEND_API_KEY"], os.environ["MAIL_TO"]
    record = Record(DB_FILE, rules["history"], SEED_FILE)
    serve(DB_FILE, rules)
    loop(record, rules, lambda subject, text: send_mail(api_key, to, subject, text))


if __name__ == "__main__":
    sys.exit(main())
