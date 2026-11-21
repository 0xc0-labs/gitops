"""Proof that a sandbox pulls the private runner image and runs its tree (README.md, The runner).

Runs with the factory worker's ServiceAccount, labels and image, so it has the
worker's rights and the worker's network. It makes a Sandbox the way the
worker's adapter does, with the runner copied in by an init container from the
private image as one directory mounted by `subPath` and the pull Secret named,
runs commands in it through sandboxd, and always deletes it. Prints one line
per step, `ok` or `FAILED`, and exits non-zero when one fails.
"""

import hashlib
import hmac
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

import grpc

SERVICE_ACCOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc"
NAMESPACE = "matchmark-sandboxes"
SANDBOXES = f"/apis/agents.x-k8s.io/v1beta1/namespaces/{NAMESPACE}/sandboxes"
SANDBOXD_IMAGE = os.environ["MATCHMARK_SANDBOX_SANDBOXD_IMAGE"]
RUNNER_IMAGE = os.environ["MATCHMARK_RUNNER_IMAGE"]
PULL_SECRET = os.environ["MATCHMARK_SANDBOX_IMAGE_PULL_SECRET"]
TOKEN_KEY = os.environ["MATCHMARK_SANDBOX_TOKEN_KEY"]
# The toolchain image of the playground's sandboxes, as runner-proof has it.
TOOLCHAIN_IMAGE = "ghcr.io/astral-sh/uv:python3.12-trixie@sha256:f41e4fb82558e309e208a59ff7f0ac1ee9e70f87df60c6f77a17ad8c028402a3"
RUNNER = "/opt/runner"
PATH = f"{RUNNER}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# The call the worker's adapter makes, with the messages encoded by hand as it
# does (field numbers: ProcessConfig {command = 1}; ExecuteWithLimitsRequest
# {config = 1, stdout_limit_bytes = 2, stderr_limit_bytes = 3, time_limit = 4
# (Duration {seconds = 1})}; ExecuteWithLimitsResponse {exit_code = 1, stdout =
# 2, stderr = 3}).
EXECUTE = "/process.v1.ProcessService/ExecuteWithLimits"
OUTPUT_LIMIT = 64 * 1024

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


def wait_for(predicate, timeout, interval=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def length_delimited(number, data):
    return varint(number << 3 | 2) + varint(len(data)) + data


def number_field(number, value):
    return varint(number << 3) + varint(value)


def read_varint(data, at):
    value = shift = 0
    while True:
        byte = data[at]
        at += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, at
        shift += 7


def decode(data):
    """(exit code, output) of an ExecuteResponse."""
    fields = {}
    at = 0
    while at < len(data):
        key, at = read_varint(data, at)
        number, wire = key >> 3, key & 7
        if wire == 0:
            fields[number], at = read_varint(data, at)
        elif wire == 2:
            size, at = read_varint(data, at)
            fields[number] = data[at : at + size]
            at += size
        else:
            raise ValueError(f"wire type {wire}")
    code = fields.get(1, 0)
    if code >= 1 << 63:
        code -= 1 << 64
    output = (fields.get(2, b"") + fields.get(3, b"")).decode("utf-8", errors="replace").strip()
    return code, output


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
                    "imagePullSecrets": [{"name": PULL_SECRET}],
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
                        },
                        {
                            "name": "install-runner",
                            "image": RUNNER_IMAGE,
                            "command": ["cp", "-a", RUNNER, "/work/"],
                            "securityContext": security,
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "64Mi"},
                                "limits": {"cpu": "1", "memory": "256Mi"},
                            },
                            "volumeMounts": [{"name": "runner", "mountPath": "/work"}],
                        },
                    ],
                    "containers": [
                        {
                            "name": "runtime",
                            "image": TOOLCHAIN_IMAGE,
                            "command": [
                                "/sandboxd-bin/sandboxd",
                                "--root-dir=/workspace",
                                "--rest-port=8080",
                                "--grpc-port=9090",
                                f"--auth-token-sha256={hashlib.sha256(token.encode()).hexdigest()}",
                            ],
                            "env": [{"name": "PATH", "value": PATH}],
                            "ports": [{"containerPort": 8080}, {"containerPort": 9090}],
                            "readinessProbe": {"httpGet": {"path": "/v1/health", "port": 8080}},
                            "securityContext": security,
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "100Mi"},
                                "limits": {"cpu": "1", "memory": "512Mi", "ephemeral-storage": "2Gi"},
                            },
                            "volumeMounts": [
                                {"name": "sandboxd-bin", "mountPath": "/sandboxd-bin", "readOnly": True},
                                {"name": "runner", "mountPath": RUNNER, "subPath": "runner", "readOnly": True},
                                {"name": "workspace", "mountPath": "/workspace"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "sandboxd-bin", "emptyDir": {"sizeLimit": "64Mi"}},
                        {"name": "runner", "emptyDir": {"sizeLimit": "1Gi"}},
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


def main():
    name = f"runner-sandbox-proof-{int(time.time())}"
    token = hmac.new(TOKEN_KEY.encode(), f"sandboxd-token:{name}".encode(), hashlib.sha256).hexdigest()
    created = False
    channel = None
    try:
        status, _ = create_when_free(sandbox_body(name, token))
        created = status in (200, 201)
        check("sandbox created", created, f"{name}, status {status}")
        if not created:
            return
        # The first pull of the private image is the slow part.
        sandbox = wait_for(lambda: sandbox_ready(name), 600)
        check("sandbox ready (private image pulled, runner copied)", sandbox is not None, name)
        if sandbox is None:
            return

        pod_name = sandbox["metadata"].get("annotations", {}).get("agents.x-k8s.io/pod-name", name)
        status, pod = kube("GET", f"/api/v1/namespaces/{NAMESPACE}/pods/{pod_name}")
        ip = pod.get("status", {}).get("podIP")
        check("pod read", status == 200 and bool(ip), f"{pod_name}, status {status}, ip {ip}")
        if not ip:
            return
        pull_secrets = [item.get("name") for item in pod.get("spec", {}).get("imagePullSecrets", [])]
        check("pull Secret named by the pod", PULL_SECRET in pull_secrets, ", ".join(pull_secrets))

        channel = grpc.insecure_channel(f"{ip}:9090")
        execute = channel.unary_unary(EXECUTE, request_serializer=lambda data: data, response_deserializer=lambda data: data)
        metadata = (("authorization", f"Bearer {token}"),)

        def run(command, timeout=60):
            config = b"".join(length_delimited(1, part.encode()) for part in command)
            request = (
                length_delimited(1, config)
                + number_field(2, OUTPUT_LIMIT)
                + number_field(3, OUTPUT_LIMIT)
                + length_delimited(4, number_field(1, timeout))
            )
            try:
                return decode(execute(request, metadata=metadata, timeout=timeout + 10))
            except (grpc.RpcError, ValueError) as error:
                return -1, f"the call failed: {error!r}"[:400]

        code, output = run([f"{RUNNER}/venv/bin/python", "-c", "import matchmark_runner"])
        check("runner imports from the tree", code == 0, output[-300:] or "imported")
        code, output = run([f"{RUNNER}/bin/bwrap", "--version"])
        check("bwrap runs from the tree", code == 0, output[-300:])

        outside_code, outside = run(["readlink", "/proc/self/ns/pid"])
        code, inside = run(
            [
                f"{RUNNER}/bin/bwrap", "--unshare-user", "--unshare-pid", "--ro-bind", "/", "/",
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--", "sh", "-c", "echo pid $$; readlink /proc/self/ns/pid",
            ]
        )
        lines = inside.splitlines()
        check(
            "bwrap makes its namespaces in a sandbox",
            outside_code == 0 and code == 0 and lines[:1] == ["pid 1"] and len(lines) == 2 and lines[1] != outside,
            inside.replace("\n", " | ")[-300:],
        )
    finally:
        if channel is not None:
            channel.close()
        if created:
            status, _ = kube("DELETE", f"{SANDBOXES}/{name}")
            gone = wait_for(lambda: kube("GET", f"{SANDBOXES}/{name}")[0] == 404, 180)
            check("sandbox deleted and none left", status in (200, 202, 404) and bool(gone), f"{name}, delete status {status}")


main()

if failures:
    print(f"FAILED: {', '.join(failures)}", flush=True)
    sys.exit(1)
print("all steps ok", flush=True)
