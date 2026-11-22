"""Mails the operator the Hetzner auction offers that meet his limits (README.md).

One pass: read the feed, compare it with what was mailed, send at most one mail,
keep the state in a ConfigMap. Standard library only. Logs one JSON line a pass
and never the API key or the address the mail goes to: errors carry an HTTP
status, not a response.

    python watch.py                          a pass, as the CronJob runs it
    python watch.py --self-test              the assertions
    python watch.py --dry-run FEED [--rules FILE] [--first-pass]
                                             print the mail a saved feed would send
"""

import json
import os
import ssl
import sys
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
# Passes in a row that cannot read the feed before the operator is told.
ALERT_AFTER = 15
RULES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")
STATE_NAME = "auction-watch-state"
KUBE_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class FeedError(Exception):
    """The feed could not be read, or has a shape this script does not know."""


class MailError(Exception):
    """Resend refused the mail, or could not be reached."""


def log(**fields):
    fields = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": "info", **fields}
    print(json.dumps(fields, separators=(",", ":")), flush=True)


# --- Pure: the feed, the rules, what to mail --------------------------------

def parse_offers(feed):
    """The offers of a feed as flat dicts. Anything unexpected is a FeedError."""
    try:
        offers = []
        for raw in feed["server"]:
            storage = raw["Hardware"]["Storage"]["Details"]
            offers.append({
                "id": int(raw["Id"]),
                "cpu": str(raw["Hardware"]["CPU"]["Name"]),
                "ram_gb": float(raw["Hardware"]["RAM"]["Size"]),
                "ecc": bool(raw["Hardware"]["RAM"].get("ecc", False)),
                "nvme": [int(size) for size in storage["nvme"]],
                "sata": [int(size) for size in storage["sata"]],
                "hdd": [int(size) for size in storage["hdd"]],
                "price": float(raw["Prices"]["monthly"]["EUR"]),
                "datacenter": str(raw["Details"]["Datacenter"]["Name"]),
                "specials": [str(s) for s in raw["Details"].get("Specials") or []],
                "next_drop_s": raw.get("Timer", {}).get("ReduceNext"),
            })
        return offers
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise FeedError(f"unknown feed shape: {type(e).__name__}: {e}") from None


def parse_rules(rules):
    try:
        return [{"name": str(r["name"]), "min_ram_gb": float(r["min_ram_gb"]),
                 "nvme_required": bool(r["nvme_required"]), "max_price_eur": float(r["max_price_eur"])}
                for r in rules]
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"rules file: {type(e).__name__}: {e}") from None


def has_shape(offer, rule):
    return offer["ram_gb"] >= rule["min_ram_gb"] and (not rule["nvme_required"] or bool(offer["nvme"]))


def meets(offer, rule):
    return has_shape(offer, rule) and offer["price"] < rule["max_price_eur"]


def plan(offers, rules, mailed):
    """What to mail, and the state of `mailed` ({id: price}) once it is mailed.

    An offer is mailed when it meets a rule and was not mailed, or was mailed
    at a higher price. One that left the feed is forgotten.
    """
    to_mail = []
    for offer in offers:
        met = [r["name"] for r in rules if meets(offer, r)]
        if met and (str(offer["id"]) not in mailed or offer["price"] < mailed[str(offer["id"])]):
            to_mail.append((offer, met))
    new_mailed = {}
    for offer in offers:
        key = str(offer["id"])
        if key in mailed:
            new_mailed[key] = mailed[key]
    for offer, _ in to_mail:
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


def describe(offer, met):
    return "\n".join([
        f"Offer {offer['id']}: {eur(offer['price'])} a month (Prices.monthly.EUR, as the feed gives it)",
        f"  Rule met: {', '.join(met)}",
        f"  Next price drop in: {duration(offer['next_drop_s'])}",
        f"  CPU: {offer['cpu']}",
        f"  RAM: {offer['ram_gb']:g} GB, {'ECC' if offer['ecc'] else 'not ECC'}",
        f"  Drives: {drives(offer)}",
        f"  Datacenter: {offer['datacenter']}",
        f"  Specials: {', '.join(offer['specials']) or 'none'}",
        f"  Where: {AUCTION_PAGE} (search for id {offer['id']})",
    ])


def cheapest_by_rule(offers, rules):
    lines = []
    for rule in rules:
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
        body.append(f"{len(to_mail)} offer(s) meet your limits. Prices are Prices.monthly.EUR.")
        body.extend(describe(o, met) for o, met in to_mail)
    cheapest = f"{len(to_mail)} offer(s) meet your limits, from {eur(min(o['price'] for o, _ in to_mail))}" if to_mail else ""
    lead = notices[0].splitlines()[0].rstrip(".") if notices else "auction-watch"
    subject = f"{lead}: {cheapest}" if notices and cheapest else lead if notices else f"{lead}: {cheapest}"
    return subject, "\n\n".join(body) + "\n"


def up_notice(offers, rules):
    return "\n".join([f"auction-watch is up: the feed holds {len(offers)} offers",
                      "The cheapest of each rule's shape:", *cheapest_by_rule(offers, rules)])


def down_notice(failures):
    return (f"auction-watch cannot read the feed: {failures} passes in a row failed\n"
            "Its log says why; no state was touched. Another mail comes when it reads again.")


def recovered_notice():
    return "auction-watch reads the feed again"


# --- Impure: the feed, the mail, the state ----------------------------------

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


class State:
    """The state, in a ConfigMap the pod reads and writes through the Kubernetes API."""

    def __init__(self, namespace):
        self.base = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT']}"
        self.path = f"/api/v1/namespaces/{namespace}/configmaps"
        self.tls = ssl.create_default_context(cafile=f"{KUBE_DIR}/ca.crt")

    def call(self, method, path, body=None, content_type="application/json"):
        with open(f"{KUBE_DIR}/token") as f:
            token = f.read().strip()
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": "Bearer " + token, "Content-Type": content_type})
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=self.tls) as r:  # nosemgrep: dynamic-urllib-use-detected - the address is the cluster's own API
            return json.loads(r.read())

    def load(self):
        """The state, or None when there is none yet: the first pass."""
        try:
            data = self.call("GET", f"{self.path}/{STATE_NAME}").get("data") or {}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        return {"mailed": json.loads(data.get("mailed", "{}")), "failures": int(data.get("failures", "0")),
                "alerted": data.get("alerted") == "true", "up": data.get("up") == "true", "last_modified": data.get("last_modified", "")}

    def save(self, state, create):
        data = {"mailed": json.dumps(state["mailed"], separators=(",", ":")), "failures": str(state["failures"]),
                "alerted": "true" if state["alerted"] else "false", "up": "true" if state["up"] else "false",
                "last_modified": state["last_modified"]}
        if create:
            self.call("POST", self.path, {"apiVersion": "v1", "kind": "ConfigMap", "data": data,
                                           "metadata": {"name": STATE_NAME}})
        else:
            self.call("PATCH", f"{self.path}/{STATE_NAME}", {"data": data}, "application/merge-patch+json")


def run_pass(state_store, rules, mail):
    """One pass. `mail(subject, text)` sends. Returns the fields of its log line."""
    started = time.monotonic()
    summary = {"msg": "pass", "offers": 0, "met": 0, "mailed": 0, "feed": "error"}
    try:
        stored = state_store.load()
        create = stored is None
        state = stored or {"mailed": {}, "failures": 0, "alerted": False, "up": False, "last_modified": ""}
        first = not state["up"]
        try:
            status, feed, last_modified = fetch_feed(state["last_modified"])
            offers = parse_offers(feed) if feed is not None else None
        except FeedError as e:
            summary.update(level="warn", error=str(e), feed="unreadable")
            state["failures"] += 1
            if state["failures"] >= ALERT_AFTER and not state["alerted"]:
                mail("auction-watch: cannot read the feed", down_notice(state["failures"]) + "\n")
                state["alerted"] = True
            state_store.save(state, create=create)
            return summary
        summary["feed"] = str(status)
        notices = []
        if state["alerted"]:
            notices.append(recovered_notice())
        if first:
            if offers is None:
                # Not up yet means no stored last-modified: never a 304.
                raise ValueError("a 304 before the first pass")
            notices.append(up_notice(offers, rules))
        to_mail = []
        if offers is not None:
            summary["offers"] = len(offers)
            summary["met"] = sum(1 for o in offers if any(meets(o, r) for r in rules))
            to_mail, state["mailed"] = plan(offers, rules, state["mailed"])
        message = build_mail(to_mail, notices)
        if message:
            mail(*message)
        summary["mailed"] = len(to_mail)
        state.update(failures=0, alerted=False, up=True, last_modified=last_modified)
        state_store.save(state, create=create)
        return summary
    except (MailError, urllib.error.URLError, OSError, ValueError, KeyError) as e:
        summary.update(level="error", error=f"{type(e).__name__}: {e}" if not isinstance(e, MailError) else str(e), failed=True)
        return summary
    finally:
        summary["seconds"] = round(time.monotonic() - started, 2)


# --- Self-test and dry run ---------------------------------------------------

def _offer(id_, ram, nvme, price):
    return {"id": id_, "cpu": "CPU", "ram_gb": ram, "ecc": False, "nvme": nvme, "sata": [], "hdd": [],
            "price": price, "datacenter": "FSN1-DC1", "specials": [], "next_drop_s": 3600}


class _Memory:
    def __init__(self, state=None):
        self.state = state

    def load(self):
        return None if self.state is None else json.loads(json.dumps(self.state))

    def save(self, state, create):
        assert create == (self.state is None)
        self.state = json.loads(json.dumps(state))


def self_test():
    global fetch_feed
    rules = parse_rules(json.load(open(RULES_FILE)))
    assert len(rules) == 2
    r64, r128 = rules
    assert (r64["min_ram_gb"], r64["nvme_required"], r64["max_price_eur"]) == (64, True, 60)
    assert (r128["min_ram_gb"], r128["nvme_required"], r128["max_price_eur"]) == (128, True, 100)

    # The rules: the price is under the limit, never equal; NVMe is needed; RAM is a floor.
    assert meets(_offer(1, 64, [512], 59.99), r64)
    assert not meets(_offer(1, 64, [512], 60), r64)
    assert not meets(_offer(1, 64, [], 30), r64)
    assert not meets(_offer(1, 32, [512], 30), r64)
    assert meets(_offer(1, 256, [512], 99), r128) and not meets(_offer(1, 64, [512], 99), r128)

    # Mailed once: an offer already mailed at this price is not mailed again.
    offers = [_offer(1, 64, [512], 55), _offer(2, 128, [1000], 90), _offer(3, 64, [512], 80)]
    to_mail, mailed = plan(offers, rules, {})
    assert [o["id"] for o, _ in to_mail] == [1, 2] and mailed == {"1": 55, "2": 90}
    assert to_mail[0][1] == [r64["name"]]
    to_mail, mailed = plan(offers, rules, mailed)
    assert to_mail == [] and mailed == {"1": 55, "2": 90}
    # A price that drops is mailed again; one that rose is not.
    to_mail, mailed = plan([_offer(1, 64, [512], 50), _offer(2, 128, [1000], 95)], rules, mailed)
    assert [o["id"] for o, _ in to_mail] == [1] and mailed == {"1": 50, "2": 90}
    # One that left the feed is forgotten, and mailed again if it comes back.
    to_mail, mailed = plan([_offer(2, 128, [1000], 90)], rules, mailed)
    assert to_mail == [] and mailed == {"2": 90}
    to_mail, _ = plan([_offer(1, 64, [512], 50)], rules, mailed)
    assert [o["id"] for o, _ in to_mail] == [1]

    # The mail says what the operator needs to decide.
    text = build_mail([(_offer(7, 64, [512, 512], 55), ["a rule"])], [])[1]
    for needle in ("Offer 7", "55 EUR", "a rule", "1h", "64 GB", "not ECC", "2 x NVME 512 GB", "FSN1-DC1", AUCTION_PAGE, "id 7"):
        assert needle in text, needle
    mixed = _offer(8, 64, [512], 55)
    mixed["sata"], mixed["hdd"] = [512, 1000, 512], [4000, 4000]
    assert drives(mixed) == "1 x NVME 512 GB, 2 x SATA SSD 512 GB, 1 x SATA SSD 1000 GB, 2 x HDD 4000 GB"
    assert drives(_offer(9, 64, [], 55)) == "none listed"
    assert build_mail([], []) is None
    up = up_notice([_offer(1, 64, [512], 65)], rules)
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
        kind, value, stamp = feed["answer"]
        if kind == "error":
            raise FeedError("down")
        return int(kind), value, stamp
    fetch_feed = fake_fetch

    def mail(subject, text):
        sent.append(subject)

    store = _Memory()
    result = run_pass(store, rules, mail)       # the first pass: up, with the offer
    assert result["mailed"] == 1 and len(sent) == 1 and "is up" in sent[0] and "1 offer(s)" in sent[0] and store.state["mailed"] == {"1": 55}
    result = run_pass(store, rules, mail)       # nothing new
    assert result["mailed"] == 0 and len(sent) == 1
    feed["answer"] = ("304", None, "t1")
    run_pass(store, rules, mail)
    assert len(sent) == 1
    feed["answer"] = ("error", None, "")
    before = json.dumps(store.state["mailed"])
    for n in range(1, ALERT_AFTER + 3):
        run_pass(store, rules, mail)
        assert json.dumps(store.state["mailed"]) == before and store.state["failures"] == n
    assert len(sent) == 2 and "cannot read" in sent[1]        # one mail, after fifteen
    feed["answer"] = ("200", good, "t2")
    run_pass(store, rules, mail)                              # one more when it reads again
    assert len(sent) == 3 and store.state["failures"] == 0 and not store.state["alerted"]

    # Resend refusing the mail fails the pass and leaves the state as it was.
    def refusing(subject, text):
        raise MailError("Resend answered HTTP 403")
    store = _Memory({"mailed": {}, "failures": 2, "alerted": False, "up": True, "last_modified": "old"})
    result = run_pass(store, rules, refusing)
    assert result.get("failed") and store.state["mailed"] == {} and store.state["failures"] == 2
    assert store.state["last_modified"] == "old"
    print("self-test: ok")


def dry_run(feed_path, rules_path, first_pass):
    rules = parse_rules(json.load(open(rules_path)))
    with open(feed_path) as f:
        offers = parse_offers(json.load(f))
    to_mail, _ = plan(offers, rules, {})
    notices = [up_notice(offers, rules)] if first_pass else []
    message = build_mail(to_mail, notices)
    if message is None:
        print("no mail: no offer meets a rule")
        return
    print("Subject:", message[0], "\n")
    print(message[1])


def main():
    args = sys.argv[1:]
    if args == ["--self-test"]:
        self_test()
        return 0
    if args[:1] == ["--dry-run"] and len(args) >= 2:
        rules_path = args[args.index("--rules") + 1] if "--rules" in args else RULES_FILE
        dry_run(args[1], rules_path, "--first-pass" in args)
        return 0
    if args:
        sys.exit(__doc__)
    namespace = os.environ["POD_NAMESPACE"]
    rules = parse_rules(json.load(open(RULES_FILE)))
    api_key, to = os.environ["RESEND_API_KEY"], os.environ["MAIL_TO"]
    summary = run_pass(State(namespace), rules, lambda subject, text: send_mail(api_key, to, subject, text))
    log(**summary)
    return 1 if summary.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
