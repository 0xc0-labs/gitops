"""Makes OpenObserve's "0xc0" dashboard folder match this directory.

Run by the dashboards Job (dashboards.yaml) after every sync that changes a
dashboard. Each *.json here is upserted by title: updated when the folder has
one by that title, created otherwise. A dashboard in the folder with no file
here is deleted: git wins. Dashboards outside the folder are never touched.

Standard library only. The root user comes from the environment
(ZO_ROOT_USER_EMAIL, ZO_ROOT_USER_PASSWORD), from Vault through Vault Secrets
Operator.
"""

import base64
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("O2_URL", "http://openobserve.openobserve.svc.cluster.local:5080")
ORG = "default"
FOLDER = "0xc0"
DIR = os.environ.get("DASHBOARDS_DIR", "/dashboards")

AUTH = "Basic " + base64.b64encode(
    f"{os.environ['ZO_ROOT_USER_EMAIL']}:{os.environ['ZO_ROOT_USER_PASSWORD']}".encode()
).decode()


def call(method, path, query=None, body=None):
    url = BASE + path + ("?" + urllib.parse.urlencode(query) if query else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": AUTH, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def wait_ready():
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "/healthz", timeout=5)
            return
        except (urllib.error.URLError, OSError):
            time.sleep(5)
    sys.exit("OpenObserve not ready after 5 minutes")


def folder_id():
    try:
        return call("GET", f"/api/v2/{ORG}/folders/dashboards/name/{FOLDER}")["folderId"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    created = call("POST", f"/api/v2/{ORG}/folders/dashboards", body={
        "name": FOLDER, "description": "Managed from gitops (platform/openobserve/dashboards): edits here are overwritten."})
    print(f"created folder {FOLDER}")
    return created["folderId"]


def main():
    wait_ready()
    fid = folder_id()
    existing = {d["title"]: d for d in call("GET", f"/api/{ORG}/dashboards",
                                            {"folder": fid, "pageSize": 1000})["dashboards"]}
    wanted = set()
    for path in sorted(glob.glob(os.path.join(DIR, "*.json"))):
        dash = json.load(open(path))
        title = dash["title"]
        wanted.add(title)
        for key in ("dashboardId", "owner", "created", "updatedAt"):
            dash.pop(key, None)
        if title in existing:
            cur = existing[title]
            dash["dashboardId"] = cur["dashboard_id"]
            call("PUT", f"/api/{ORG}/dashboards/{cur['dashboard_id']}",
                 {"folder": fid, "hash": cur["hash"]}, dash)
            print(f"updated {title}")
        else:
            call("POST", f"/api/{ORG}/dashboards", {"folder": fid}, dash)
            print(f"created {title}")
    for title, cur in existing.items():
        if title not in wanted:
            call("DELETE", f"/api/{ORG}/dashboards/{cur['dashboard_id']}", {"folder": fid})
            print(f"deleted {title}")


if __name__ == "__main__":
    main()
