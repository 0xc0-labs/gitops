"""Proof of what the factory worker has been given (README.md, Artifacts and
The factory worker).

Runs with the worker's ServiceAccount and labels, so it has the worker's
rights and the worker's network, and with the App's key mounted as the worker
has it. Prints one line per step, exits non-zero when one fails, and always
deletes the Sandbox and the objects it made. It never prints the key, the JWT
or a token.
"""

import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

from s3 import Client, md5_header

SERVICE_ACCOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc"
NAMESPACE = "matchmark-sandboxes"
SANDBOXES = f"/apis/agents.x-k8s.io/v1beta1/namespaces/{NAMESPACE}/sandboxes"
SANDBOXD_IMAGE = os.environ["MATCHMARK_SANDBOX_SANDBOXD_IMAGE"]
TOKEN_KEY = os.environ["MATCHMARK_SANDBOX_TOKEN_KEY"]
BUCKET = os.environ["MATCHMARK_ARTIFACTS_BUCKET"]
SANDBOX_IMAGE = "docker.io/library/python:3.13.9-slim-bookworm@sha256:b685a4fa58bb19d1814d78a1ec0f0208f351452724f78b20212c984d6e124a34"

LIBS = "/work/lib"
GITHUB = "api.github.com"
REPOSITORY = "0xc0-labs/matchmark-playground"
# The rule that lets the worker into the proxy belongs to another Application,
# which can sync after this Job starts.
PROXY_WAIT = 300
# Plane's Application can sync after this Job starts, as the proxy's can.
TRACKER_WAIT = 300
PROJECT = "PLAY"
# The names the playground's mapping uses (matchmark-config).
STATES = ("Backlog", "Todo", "In Progress", "In Review", "Blocked", "Done", "Cancelled", "Spec Review", "Approved")
PERMISSIONS = {"metadata": "read", "contents": "write", "pull_requests": "write", "checks": "write"}

failures = []
context = ssl.create_default_context(cafile=f"{SERVICE_ACCOUNT}/ca.crt")


def check(step, ok, detail=""):
    print(f"{step}: {'ok' if ok else 'FAILED'} ({detail})", flush=True)
    if not ok:
        failures.append(step)


def kube(method, path, body=None):
    """(status, parsed body) of a call to the API server with the pod's token."""
    with open(f"{SERVICE_ACCOUNT}/token") as handle:
        token = handle.read().strip()
    request = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, context=context, timeout=30) as response:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected -- the URL is the API server's fixed address plus a path this script builds
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.load(error)
        except ValueError:
            return error.code, {}


def runtime(ip, token=None):
    """Status of sandboxd's metadata call on 8080, with the token or without."""
    request = urllib.request.Request(f"http://{ip}:8080/v1/metadata")  # nosemgrep: python.lang.security.audit.insecure-transport.urllib.insecure-request-object.insecure-request-object -- sandboxd serves plain HTTP on the pod network; the policy is the boundary, the token the second wall
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # nosemgrep: python.lang.security.audit.insecure-transport.urllib.insecure-urlopen.insecure-urlopen, python.lang.security.audit.dynamic-urllib-use-detected -- same
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def wait_for(predicate, timeout, interval=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def sandbox_body(name, token):
    security = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    return {
        "apiVersion": "agents.x-k8s.io/v1beta1",
        "kind": "Sandbox",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": {
            "podTemplate": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "matchmark-sandbox",
                        "matchmark.0xc0.cc/sandbox": "true",
                    }
                },
                "spec": {
                    "runtimeClassName": "gvisor",
                    "priorityClassName": "matchmark-batch",
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "restartPolicy": "Always",
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "initContainers": [
                        {
                            "name": "install-sandboxd",
                            "image": SANDBOXD_IMAGE,
                            "command": ["/bin/sh", "-c", "cp /usr/local/bin/sandboxd /sandboxd-bin/sandboxd"],
                            "securityContext": security,
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "32Mi"},
                                "limits": {"cpu": "100m", "memory": "64Mi"},
                            },
                            "volumeMounts": [{"name": "sandboxd-bin", "mountPath": "/sandboxd-bin"}],
                        }
                    ],
                    "containers": [
                        {
                            "name": "runtime",
                            "image": SANDBOX_IMAGE,
                            "command": [
                                "/sandboxd-bin/sandboxd",
                                "--root-dir=/workspace",
                                "--rest-port=8080",
                                "--grpc-port=9090",
                                f"--auth-token-sha256={hashlib.sha256(token.encode()).hexdigest()}",
                            ],
                            "ports": [{"containerPort": 8080}, {"containerPort": 9090}],
                            "readinessProbe": {"httpGet": {"path": "/v1/health", "port": 8080}},
                            "securityContext": security,
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "100Mi"},
                                "limits": {"cpu": "1", "memory": "256Mi", "ephemeral-storage": "1Gi"},
                            },
                            "volumeMounts": [
                                {"name": "sandboxd-bin", "mountPath": "/sandboxd-bin", "readOnly": True},
                                {"name": "workspace", "mountPath": "/workspace"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "sandboxd-bin", "emptyDir": {"sizeLimit": "64Mi"}},
                        {"name": "workspace", "emptyDir": {"sizeLimit": "500Mi"}},
                    ],
                },
            }
        },
    }


def create_when_free(body, timeout=3000):
    """The quota is one Sandbox: wait while another proof holds it."""
    deadline = time.monotonic() + timeout
    while True:
        status, answer = kube("POST", SANDBOXES, body)
        if status in (200, 201):
            return status, answer
        if status != 403 or "exceeded quota" not in json.dumps(answer) or time.monotonic() > deadline:
            return status, answer
        time.sleep(10)


def sandbox_ready(name):
    status, sandbox = kube("GET", f"{SANDBOXES}/{name}")
    if status != 200:
        return None
    for condition in sandbox.get("status", {}).get("conditions", []):
        if condition["type"] == "Ready" and condition["status"] == "True":
            return sandbox
    return None


def sandboxes_proof():
    for host in (
        "egress-proxy.matchmark-sandboxes.svc.cluster.local",
        "gateway.matchmark.svc.cluster.local",
    ):
        try:
            socket.getaddrinfo(host, None)
            check(f"resolves {host.split('.')[0]}", True, host)
        except OSError as error:
            check(f"resolves {host.split('.')[0]}", False, str(error))

    name = f"worker-proof-{int(time.time())}"
    token = hmac.new(TOKEN_KEY.encode(), f"sandboxd-token:{name}".encode(), hashlib.sha256).hexdigest()
    created = False
    try:
        status, answer = create_when_free(sandbox_body(name, token))
        created = status in (200, 201)
        check("sandbox created", created, f"{name}, status {status}")
        if not created:
            return
        sandbox = wait_for(lambda: sandbox_ready(name), 420)
        check("sandbox ready", sandbox is not None, name)
        if sandbox is None:
            return

        pod_name = sandbox["metadata"].get("annotations", {}).get("agents.x-k8s.io/pod-name", name)
        status, pod = kube("GET", f"/api/v1/namespaces/{NAMESPACE}/pods/{pod_name}")
        ip = pod.get("status", {}).get("podIP")
        check("pod read", status == 200 and bool(ip), f"{pod_name}, status {status}, ip {ip}")
        if not ip:
            return
        check("runtime reached with the derived token", runtime(ip, token) == 200, f"{ip}:8080")
        refused = runtime(ip)
        check("runtime refuses a call without the token", refused in (401, 403), f"status {refused}")

        status, _ = kube("GET", SANDBOXES)
        check("list of sandboxes refused", status == 403, f"status {status}")
        status, _ = kube("GET", f"/api/v1/namespaces/{NAMESPACE}/secrets/claude-subscription-vault")
        check("get of a Secret there refused", status == 403, f"status {status}")
    finally:
        if created:
            status, _ = kube("DELETE", f"{SANDBOXES}/{name}")
            gone = wait_for(lambda: kube("GET", f"{SANDBOXES}/{name}")[0] == 404, 180)
            check("sandbox deleted", status in (200, 202, 404) and bool(gone), f"delete status {status}")


def upload_id_of(body):
    found = re.search(rb"<UploadId>([^<]+)</UploadId>", body)
    return found.group(1).decode() if found else None


def etag_of(headers):
    return next((value for name, value in headers.items() if name.lower() == "etag"), "")


def artifacts_proof():
    client = Client(os.environ["MATCHMARK_ARTIFACTS_ACCESS_KEY_ID"], os.environ["MATCHMARK_ARTIFACTS_SECRET_ACCESS_KEY"])
    prefix = f"proof/{int(time.time())}"
    plain, multi, aborted = f"{prefix}/plain", f"{prefix}/multipart", f"{prefix}/aborted"
    data = os.urandom(2048)
    try:
        status, _, _ = client.request("PUT", BUCKET, plain, body=data)
        check("object put", status == 200, f"status {status}")
        status, body, _ = client.request("GET", BUCKET, plain)
        check("object read back", status == 200 and body == data, f"status {status}")

        status, body, _ = client.request("POST", BUCKET, multi, query={"uploads": ""})
        upload_id = upload_id_of(body) if status == 200 else None
        check("multipart upload started", bool(upload_id), f"status {status}")
        if upload_id:
            status, _, headers = client.request(
                "PUT", BUCKET, multi, query={"partNumber": "1", "uploadId": upload_id}, body=data
            )
            check("part uploaded", status == 200, f"status {status}")
            complete = (
                "<CompleteMultipartUpload><Part><PartNumber>1</PartNumber>"
                f"<ETag>{etag_of(headers)}</ETag></Part></CompleteMultipartUpload>"
            ).encode()
            status, body, _ = client.request("POST", BUCKET, multi, query={"uploadId": upload_id}, body=complete)
            # A 200 can still carry an <Error> body.
            check("multipart upload completed", status == 200 and b"<Error>" not in body, f"status {status}")
            status, body, _ = client.request("GET", BUCKET, multi)
            check("multipart object read back", status == 200 and body == data, f"status {status}")

        status, body, _ = client.request("POST", BUCKET, aborted, query={"uploads": ""})
        second = upload_id_of(body) if status == 200 else None
        if second:
            status, _, _ = client.request("DELETE", BUCKET, aborted, query={"uploadId": second})
            check("multipart upload aborted", status in (200, 204), f"status {status}")
        else:
            check("multipart upload aborted", False, "no upload to abort")

        status, body, _ = client.request("GET", BUCKET, query={"list-type": "2", "prefix": prefix + "/"})
        check("bucket listed", status == 200 and plain.encode() in body, f"status {status}")
    finally:
        keys = "".join(f"<Object><Key>{key}</Key></Object>" for key in (plain, multi))
        delete = f"<Delete><Quiet>true</Quiet>{keys}</Delete>".encode()
        status, body, _ = client.request(
            "POST", BUCKET, query={"delete": ""}, body=delete, headers={"content-md5": md5_header(delete)}
        )
        check("objects deleted", status == 200 and b"<Error>" not in body, f"status {status}")
        status, body, _ = client.request("GET", BUCKET, query={"list-type": "2", "prefix": "proof/"})
        check("no object left behind", status == 200 and b"<Key>" not in body, f"status {status}")

    other = f"proof-other-{int(time.time())}"
    status, _, _ = client.request("PUT", other)
    check("second bucket not created", status == 403, f"status {status}")
    status, _, _ = client.request("GET", other, query={"list-type": "2"})
    check("second bucket not listed", status == 403, f"status {status}")


def accepts(host, port):
    try:
        socket.create_connection((host, port), timeout=5).close()
    except OSError:
        return False
    return True


def run(proof):
    try:
        proof()
    except Exception as error:  # noqa: BLE001 - whatever a step raises must end as a line of the list
        check(proof.__name__, False, f"{type(error).__name__}: {error}")


def github_call(method, path, bearer=None, host=GITHUB):
    """(status, parsed body) of a call to GitHub through the proxy's CONNECT tunnel."""
    proxy = urlsplit(os.environ["MATCHMARK_GITHUB_PROXY_URL"])
    connection = http.client.HTTPSConnection(  # nosemgrep: python.lang.security.audit.httpsconnection-detected -- Python 3.13 verifies certificates and the context is the default one; only HTTPSConnection tunnels CONNECT
        proxy.hostname, proxy.port, timeout=30, context=ssl.create_default_context()
    )
    connection.set_tunnel(host, 443)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "matchmark-worker-proof"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        try:
            return response.status, json.loads(raw) if raw else {}
        except ValueError:
            return response.status, {}
    finally:
        connection.close()


def app_jwt(key_path):
    """The App's JWT, RS256, valid for ten minutes at most."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")

    now = int(time.time())
    signing_input = part({"alg": "RS256", "typ": "JWT"}) + b"." + part(
        {"iat": now - 60, "exp": now + 540, "iss": os.environ["MATCHMARK_GITHUB_APP_ID"]}
    )
    with open(key_path, "rb") as handle:
        key = serialization.load_pem_private_key(handle.read(), password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return (signing_input + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


def github_proof():
    key_path = os.environ["MATCHMARK_GITHUB_APP_KEY_FILE"]
    installation = os.environ["MATCHMARK_GITHUB_INSTALLATION_ID"]
    proxy_url = os.environ["MATCHMARK_GITHUB_PROXY_URL"]
    proxy = urlsplit(proxy_url)

    mode = os.stat(key_path).st_mode & 0o777
    check(
        "key is a file only its user and group read",
        os.path.isfile(key_path) and mode & 0o007 == 0 and mode & 0o222 == 0,
        f"mode {mode:04o}",
    )
    check("key is in no variable", not any("PRIVATE KEY" in value for value in os.environ.values()), "environment")

    reachable = wait_for(lambda: accepts(proxy.hostname, proxy.port), PROXY_WAIT)
    check("proxy accepts a connection", bool(reachable), f"{proxy.hostname}:{proxy.port}, waited up to {PROXY_WAIT}s")
    if not reachable:
        return

    status, _ = github_call("GET", "/zen")
    check("api.github.com answers through the proxy", status == 200, f"status {status}")
    try:
        github_call("GET", "/", host="example.com")
        check("host outside the list refused", False, "the tunnel opened")
    except OSError as error:
        check("host outside the list refused", "403" in str(error), str(error))
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=5).close()
        check("direct connection to a public address fails", False, "connected")
    except OSError as error:
        check("direct connection to a public address fails", True, type(error).__name__)
    try:
        socket.create_connection((proxy.hostname, 22), timeout=5).close()
        check("proxy not reached on another port", False, "connected to 22")
    except OSError as error:
        check("proxy not reached on another port", True, type(error).__name__)

    # The worker's network reaches the proxy and nothing else, so the library
    # that signs the JWT comes through the proxy, as the sandbox proof's do.
    installed = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit -- pinned arguments, nothing from outside
        [
            sys.executable, "-m", "pip", "install", "--quiet", "--no-cache-dir",
            "--disable-pip-version-check", "--target", LIBS, "cryptography==46.0.3",
        ],
        env={**os.environ, "HTTPS_PROXY": proxy_url, "HTTP_PROXY": proxy_url},
        capture_output=True,
        check=False,
    )
    check("pip installs through the proxy", installed.returncode == 0, f"exit {installed.returncode}")
    if installed.returncode != 0:
        return
    sys.path.insert(0, LIBS)

    try:
        token_jwt = app_jwt(key_path)
    except Exception as error:  # noqa: BLE001 - the message of a key error can quote the key
        check("JWT signed with the mounted key", False, type(error).__name__)
        return
    check("JWT signed with the mounted key", True, "RS256")

    status, app = github_call("GET", "/app", bearer=token_jwt)
    check("the App answers to its JWT", status == 200, f"status {status}")
    if status == 200:
        check("the App's permissions are the design's four", app.get("permissions") == PERMISSIONS, json.dumps(app.get("permissions")))
        check("the App has no events", app.get("events") == [], json.dumps(app.get("events")))

    status, found = github_call("GET", f"/app/installations/{installation}", bearer=token_jwt)
    check("the installation answers", status == 200, f"status {status}")
    if status == 200:
        check("installation covers selected repositories", found.get("repository_selection") == "selected", str(found.get("repository_selection")))
        check("installation's permissions are the four", found.get("permissions") == PERMISSIONS, json.dumps(found.get("permissions")))

    status, granted = github_call("POST", f"/app/installations/{installation}/access_tokens", bearer=token_jwt)
    token = granted.get("token") if status == 201 else None
    check("token made for the installation", bool(token), f"status {status}")
    if not token:
        return
    try:
        status, listed = github_call("GET", "/installation/repositories", bearer=token)
        names = [repository.get("full_name") for repository in listed.get("repositories", [])]
        check(
            f"installation's only repository is {REPOSITORY}",
            status == 200 and names == [REPOSITORY] and listed.get("total_count") == 1,
            f"status {status}, {len(names)} repositories: {', '.join(names)}",
        )
    finally:
        status, _ = github_call("DELETE", "/installation/token", bearer=token)
        check("token revoked", status == 204, f"status {status}")


def tracker_call(path, key):
    """(status, parsed body) of a GET to Plane's API with the platform's key."""
    request = urllib.request.Request(  # nosemgrep: python.lang.security.audit.insecure-transport.urllib.insecure-request-object.insecure-request-object -- Plane's API is plain HTTP on the pod network; the policies are the boundary
        os.environ["MATCHMARK_TRACKER_ADDRESS"].rstrip("/") + path,
        headers={"X-API-Key": key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # nosemgrep: python.lang.security.audit.insecure-transport.urllib.insecure-urlopen.insecure-urlopen, python.lang.security.audit.dynamic-urllib-use-detected -- same
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, {}


def results(body):
    """Plane lists come as a bare array or as a page with `results`."""
    return body if isinstance(body, list) else body.get("results", [])


ENTRY_FIELDS = ("actor", "field", "old_value", "new_value", "old_identifier", "new_identifier", "verb", "created_at", "epoch")
JSON_TYPES = {type(None): "null", bool: "boolean", int: "number", float: "number", str: "string", list: "array", dict: "object"}


def record(step, detail):
    """A line that reports and never fails the Job: a failed Job holds the Application's later syncs."""
    print(f"{step}: recorded ({detail})", flush=True)


def state_author_proof(workspace, project_id, key, own_id, state_ids):
    """Whether the tracker tells who moved an item to its state, and when. Prints types, counts and yes or no only."""
    step = "state change author"
    try:
        base = f"/api/v1/workspaces/{workspace}/projects/{project_id}/work-items/"
        status, body = tracker_call(base + "?order_by=-updated_at&per_page=10", key)
        items = [item for item in results(body) if item.get("id")][:10]
        if status != 200 or not items:
            record(step, f"work items: status {status}, {len(items)} items")
            return
        statuses, entries = set(), []
        for item in items:
            status, body = tracker_call(f"{base}{item['id']}/activities/?order_by=-created_at&per_page=100", key)
            statuses.add(status)
            entries.extend(entry for entry in results(body) if isinstance(entry, dict))
        record(step, f"activities: status {', '.join(str(code) for code in sorted(statuses))}, {len(items)} items, {len(entries)} entries")
        changes = [entry for entry in entries if entry.get("field") == "state"]
        record(step, f"{len(changes)} entries with field state")
        if not changes:
            return
        newest = max(changes, key=lambda entry: str(entry.get("created_at") or ""))
        record(step, f"newest keys: {', '.join(sorted(newest))}")
        record(
            step,
            "newest types: "
            + ", ".join(f"{name} {JSON_TYPES.get(type(newest[name]), 'other') if name in newest else 'missing'}" for name in ENTRY_FIELDS),
        )
        record(step, f"newest new_identifier is a state of the project: {'yes' if newest.get('new_identifier') in state_ids else 'no'}")
        others = any(entry.get("actor") and entry.get("actor") != own_id for entry in changes)
        record(step, f"a state entry by an actor other than the platform's user: {'yes' if others else 'no'}")
    except Exception as error:  # noqa: BLE001 - this step records; it never fails the Job
        record(step, f"stopped: {type(error).__name__}")


def moment(value):
    """Seconds since the epoch of an ISO 8601 timestamp, or None when it is missing or not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def newest_moment(entries):
    """The latest timestamp among the entries' `created_at`, or None."""
    moments = [moment(entry.get("created_at")) for entry in entries if isinstance(entry, dict)]
    moments = [value for value in moments if value is not None]
    return max(moments) if moments else None


COMMENT_WINDOW = 2.0


def comment_clock_proof(workspace, project_id, key):
    """Whether a comment moves a work item's updated_at, and how far updated_at is from the last state change. Prints counts and seconds only."""
    step = "comment updated_at"
    try:
        base = f"/api/v1/workspaces/{workspace}/projects/{project_id}/work-items/"
        status, body = tracker_call(base + "?order_by=-updated_at&per_page=10", key)
        listed = [item for item in results(body) if item.get("id")][:10]
        if status != 200 or not listed:
            record(step, f"work items: status {status}, {len(listed)} items")
            return
        read, with_comments = 0, 0
        moved = unmoved = neither = 0
        differences = []
        missing = {"updated_at": 0, "activities": 0, "comments": 0}
        statuses = set()
        for entry in listed:
            item_path = f"{base}{entry['id']}/"
            status, item = tracker_call(item_path, key)
            if status != 200 or not isinstance(item, dict):
                statuses.add(f"item {status}")
                continue
            status, body = tracker_call(f"{item_path}activities/?order_by=-created_at&per_page=100", key)
            if status != 200:
                statuses.add(f"activities {status}")
                continue
            activities = [activity for activity in results(body) if isinstance(activity, dict)]
            status, body = tracker_call(f"{item_path}comments/?order_by=-created_at&per_page=100", key)
            if status != 200:
                statuses.add(f"comments {status}")
                continue
            comments = [comment for comment in results(body) if isinstance(comment, dict)]
            read += 1
            with_comments += 1 if comments else 0
            updated = moment(item.get("updated_at"))
            last_entry = newest_moment(activities)
            last_comment = newest_moment(comments)
            if updated is None:
                missing["updated_at"] += 1
                continue
            if not activities or last_entry is None:
                missing["activities"] += 1
            if comments and last_comment is None:
                missing["comments"] += 1
            if last_comment is not None and last_entry is not None and last_comment > last_entry:
                if abs(updated - last_comment) <= COMMENT_WINDOW:
                    moved += 1
                elif abs(updated - last_entry) <= COMMENT_WINDOW:
                    unmoved += 1
                else:
                    neither += 1
            newest = max(
                (activity for activity in activities if moment(activity.get("created_at")) is not None),
                key=lambda activity: moment(activity["created_at"]),
                default=None,
            )
            if newest is not None and newest.get("field") == "state" and (last_comment is None or last_comment <= last_entry):
                differences.append(updated - moment(newest["created_at"]))
        if statuses:
            record(step, f"calls not answered 200: {', '.join(sorted(statuses))}")
        record(step, f"{read} items read, {with_comments} with at least one comment")
        record(
            step,
            f"newest comment later than the newest listed entry: {moved + unmoved + neither} items; "
            f"updated_at within {COMMENT_WINDOW:.0f}s of the comment: {moved}; "
            f"of the entry: {unmoved}; of neither: {neither}",
        )
        if differences:
            record(
                step,
                f"updated_at minus the last state change, {len(differences)} items with no later comment: "
                f"smallest {min(differences):+.1f}s, largest {max(differences):+.1f}s",
            )
        else:
            record(step, "no item whose newest listed entry is a state change with no later comment")
        gaps = [f"{name} missing on {count} items" for name, count in missing.items() if count]
        if gaps:
            record(step, "; ".join(gaps))
    except Exception as error:  # noqa: BLE001 - this step records; it never fails the Job
        record(step, f"stopped: {type(error).__name__}")


def tracker_proof():
    key_path = os.environ["MATCHMARK_TRACKER_API_KEY_FILE"]
    workspace = os.environ["MATCHMARK_TRACKER_WORKSPACE"]
    address = urlsplit(os.environ["MATCHMARK_TRACKER_ADDRESS"])

    mode = os.stat(key_path).st_mode & 0o777
    check(
        "tracker key is a file only its user and group read",
        os.path.isfile(key_path) and mode & 0o007 == 0 and mode & 0o222 == 0,
        f"mode {mode:04o}",
    )
    check("tracker key is in no variable", not any(name.endswith("API_KEY") for name in os.environ), "environment")
    with open(key_path) as handle:
        key = handle.read().strip()

    port = address.port or 80
    reachable = wait_for(lambda: accepts(address.hostname, port), TRACKER_WAIT)
    check("Plane's API accepts a connection", bool(reachable), f"{address.hostname}:{port}, waited up to {TRACKER_WAIT}s")
    if not reachable:
        return

    status, me = tracker_call("/api/v1/users/me/", key)
    check("Plane answers the key", status == 200, f"status {status}")
    if status != 200:
        return

    status, body = tracker_call(f"/api/v1/workspaces/{workspace}/projects/", key)
    project = next((item for item in results(body) if item.get("identifier") == PROJECT), None)
    check(f"project {PROJECT} is in workspace {workspace}", status == 200 and project is not None, f"status {status}")
    if project is None:
        return

    status, body = tracker_call(f"/api/v1/workspaces/{workspace}/projects/{project['id']}/states/", key)
    names = {item.get("name") for item in results(body)}
    state_ids = {item.get("id") for item in results(body)}
    missing = [name for name in STATES if name not in names]
    check(f"project {PROJECT} has every state the mapping uses", status == 200 and not missing, f"status {status}, missing: {', '.join(missing) or 'none'}")
    state_author_proof(workspace, project["id"], key, me.get("id") if isinstance(me, dict) else None, state_ids)
    comment_clock_proof(workspace, project["id"], key)


run(sandboxes_proof)
run(artifacts_proof)
run(github_proof)
run(tracker_proof)

if failures:
    print(f"FAILED: {', '.join(failures)}", flush=True)
    sys.exit(1)
print("all steps ok", flush=True)
