"""Proof of what the factory worker has been given (README.md, Artifacts).

Runs with the worker's ServiceAccount and labels, so it has the worker's
rights and the worker's network. Prints one line per step, exits non-zero when
one fails, and always deletes the Sandbox and the objects it made.
"""

import hashlib
import hmac
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

from s3 import Client, md5_header

SERVICE_ACCOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc"
NAMESPACE = "matchmark-sandboxes"
SANDBOXES = f"/apis/agents.x-k8s.io/v1beta1/namespaces/{NAMESPACE}/sandboxes"
SANDBOXD_IMAGE = os.environ["MATCHMARK_SANDBOX_SANDBOXD_IMAGE"]
TOKEN_KEY = os.environ["MATCHMARK_SANDBOX_TOKEN_KEY"]
BUCKET = os.environ["MATCHMARK_ARTIFACTS_BUCKET"]
SANDBOX_IMAGE = "docker.io/library/python:3.13.9-slim-bookworm@sha256:b685a4fa58bb19d1814d78a1ec0f0208f351452724f78b20212c984d6e124a34"

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


sandboxes_proof()
artifacts_proof()

if failures:
    print(f"FAILED: {', '.join(failures)}", flush=True)
    sys.exit(1)
print("all steps ok", flush=True)
