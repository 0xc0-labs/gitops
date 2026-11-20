"""Proof that a sandbox runs as designed (README.md, The proof).

Creates one Sandbox the way the factory worker will, runs commands in it
through sandboxd and prints one line per step. Exits non-zero when a step
fails, and always deletes what it created.
"""

import os
import subprocess
import sys
import time

LIBS = "/work/lib"
PROXY_HOST = os.environ["EGRESS_PROXY_SERVICE_HOST"]
PROXY_PORT = os.environ["EGRESS_PROXY_SERVICE_PORT"]
PROXY = f"http://{PROXY_HOST}:{PROXY_PORT}"
NAMESPACE = "matchmark-sandboxes"
SANDBOX_IMAGE = os.environ["SANDBOX_IMAGE"]
SANDBOXD_IMAGE = os.environ["SANDBOXD_IMAGE"]
GROUP, VERSION, PLURAL = "agents.x-k8s.io", "v1beta1", "sandboxes"
MEMORY_PROBE_STEP_MIB = 256
# The sandbox's memory limit is 4Gi: the probe goes one step past it.
MEMORY_PROBE_MAX_MIB = 4352
MEMORY_PROBE_MIN_NODE_FREE_GIB = 8

failures = []


def check(step, ok, detail=""):
    print(f"{step}: {'ok' if ok else 'FAILED'} ({detail})", flush=True)
    if not ok:
        failures.append(step)


def record(name, value):
    print(f"record {name}: {value}", flush=True)


# The libraries come through the proxy as the sandbox's own installs do: this
# pod carries the sandbox network label, so it reaches nothing else.
subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit -- pinned arguments, nothing from outside
    [
        sys.executable, "-m", "pip", "install", "--quiet", "--no-cache-dir",
        "--disable-pip-version-check", "--target", LIBS,
        "k8s-agent-sandbox[grpc]==1.0.11", "kubernetes==37.0.3",
    ],
    check=True,
    env={**os.environ, "HTTPS_PROXY": PROXY, "HTTP_PROXY": PROXY, "HOME": "/work"},
)
sys.path.insert(0, LIBS)

import grpc  # noqa: E402
import requests  # noqa: E402
from k8s_agent_sandbox.commands._process_stubs import process_pb2, process_pb2_grpc  # noqa: E402
from kubernetes import client, config  # noqa: E402
from kubernetes.client.rest import ApiException  # noqa: E402

config.load_incluster_config()
core = client.CoreV1Api()
custom = client.CustomObjectsApi()


def gateway_address():
    try:
        service = core.read_namespaced_service("gateway", "matchmark")
        return service.spec.cluster_ip
    except ApiException as error:
        print(f"gateway address: unavailable (status {error.status})", flush=True)
        return None


def sandbox_body(name, gateway):
    no_proxy = "127.0.0.1,localhost" + (f",{gateway}" if gateway else "")
    security = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
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
                            ],
                            "env": [
                                {"name": "HTTPS_PROXY", "value": PROXY},
                                {"name": "HTTP_PROXY", "value": PROXY},
                                {"name": "NO_PROXY", "value": no_proxy},
                                {"name": "NODE_USE_ENV_PROXY", "value": "1"},
                                {"name": "HOME", "value": "/tmp"},
                                {"name": "PIP_CACHE_DIR", "value": "/cache/pip"},
                                {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                            ],
                            "ports": [{"containerPort": 8080}, {"containerPort": 9090}],
                            "readinessProbe": {"httpGet": {"path": "/v1/health", "port": 8080}},
                            "securityContext": security,
                            "resources": {
                                "requests": {"cpu": "1", "memory": "2Gi", "ephemeral-storage": "1Gi"},
                                "limits": {"cpu": "2", "memory": "4Gi", "ephemeral-storage": "10Gi"},
                            },
                            "volumeMounts": [
                                {"name": "sandboxd-bin", "mountPath": "/sandboxd-bin", "readOnly": True},
                                {"name": "workspace", "mountPath": "/workspace"},
                                {"name": "tmp", "mountPath": "/tmp"},
                                {"name": "cache", "mountPath": "/cache"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "sandboxd-bin", "emptyDir": {"sizeLimit": "64Mi"}},
                        {"name": "workspace", "emptyDir": {"sizeLimit": "5Gi"}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
                        {"name": "cache", "emptyDir": {"sizeLimit": "2Gi"}},
                    ],
                },
            }
        },
    }


def wait_for(predicate, timeout, interval=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def sandbox_ready(name):
    try:
        sandbox = custom.get_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURAL, name)
    except ApiException:
        return None
    for condition in sandbox.get("status", {}).get("conditions", []):
        if condition["type"] == "Ready" and condition["status"] == "True":
            return sandbox
    return None


def sandbox_pod(name):
    for pod in core.list_namespaced_pod(NAMESPACE).items:
        for owner in pod.metadata.owner_references or []:
            if owner.kind == "Sandbox" and owner.name == name:
                return pod
    return None


def run(channel_stub, command, timeout=60):
    """A command through sandboxd's gRPC ProcessService: (exit code, output)."""
    request = process_pb2.ExecuteRequest(config=process_pb2.ProcessConfig(command=command))
    response = channel_stub.Execute(request, timeout=timeout)
    output = (response.stdout + response.stderr).decode("utf-8", errors="replace").strip()
    return response.exit_code, output


def node_memory(node_name):
    """(allocatable GiB, working set GiB) of the node, or None."""
    try:
        node = core.read_node(node_name)
        usage = custom.get_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes", node_name)
    except ApiException as error:
        print(f"node memory: unavailable (status {error.status})", flush=True)
        return None
    return gib(node.status.allocatable["memory"]), gib(usage["usage"]["memory"])


def gib(quantity):
    units = {"Ki": 1 / 1024**2, "Mi": 1 / 1024, "Gi": 1.0}
    for suffix, factor in units.items():
        if quantity.endswith(suffix):
            return float(quantity[: -len(suffix)]) * factor
    return float(quantity) / 1024**3


CPU_PROBE = """
import os, time
start = time.time()
children = []
for _ in range(4):
    pid = os.fork()
    if pid == 0:
        end = time.time() + 6
        while time.time() < end:
            pass
        os._exit(0)
    children.append(pid)
for pid in children:
    os.waitpid(pid, 0)
wall = time.time() - start
times = os.times()
cpu = times.children_user + times.children_system
print(f"{cpu / wall:.2f} {os.cpu_count()}")
"""

MEMORY_PROBE = f"""
import sys
held = []
for step in range(1, {MEMORY_PROBE_MAX_MIB // MEMORY_PROBE_STEP_MIB} + 1):
    held.append(bytearray(b"x" * ({MEMORY_PROBE_STEP_MIB} * 1024 * 1024)))
print({MEMORY_PROBE_MAX_MIB})
"""

DOWNLOAD_REFUSED = (
    "import urllib.request; "
    "urllib.request.urlopen('https://example.com', timeout=20)"
)
IGNORES_PROXY = (
    "import socket; "
    "socket.create_connection(('1.1.1.1', 443), timeout=8)"
)
LOOKUP = "import socket; socket.getaddrinfo('pypi.org', 443)"


def gateway_reachable(address):
    return (
        "import socket; "
        f"socket.create_connection(('{address}', 8080), timeout=8).close()"
    )


name = f"proof-{int(time.time())}"
second = f"{name}-second"
gateway = gateway_address()
created = []

try:
    custom.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURAL, sandbox_body(name, gateway))
    created.append(name)
    sandbox = wait_for(lambda: sandbox_ready(name), 420)
    check("sandbox ready", sandbox is not None, name)
    if sandbox is None:
        raise SystemExit(1)

    pod = sandbox_pod(name)
    check(
        "gvisor runtime class",
        pod is not None and pod.spec.runtime_class_name == "gvisor",
        f"pod {pod.metadata.name if pod else None}, node {pod.spec.node_name if pod else None}",
    )
    ip = pod.status.pod_ip
    # sandboxd serves plain HTTP and gRPC on the pod network; the policy is the boundary.
    health = requests.get(f"http://{ip}:8080/v1/health", timeout=10)  # nosemgrep: python.lang.security.audit.insecure-transport.requests.request-with-http
    metadata = requests.get(f"http://{ip}:8080/v1/metadata", timeout=10)  # nosemgrep: python.lang.security.audit.insecure-transport.requests.request-with-http
    check("sandboxd rest", health.ok and metadata.ok, f"health {health.status_code}, metadata {metadata.status_code}")

    stub = process_pb2_grpc.ProcessServiceStub(grpc.insecure_channel(f"{ip}:9090"))
    code, output = run(stub, ["/bin/sh", "-c", "echo hello from the sandbox; id -u; uname -r"])
    lines = output.splitlines()
    check(
        "command through sandboxd",
        code == 0 and lines[:1] == ["hello from the sandbox"] and lines[1:2] == ["10001"],
        output.replace("\n", " | "),
    )
    check("kernel is gvisor", len(lines) == 3 and "gvisor" in lines[2], lines[2] if len(lines) == 3 else output)

    code, output = run(
        stub,
        ["python", "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--target", "/tmp/site", "six==1.17.0"],
        timeout=120,
    )
    check("pip install through the proxy", code == 0, output[-200:] or "installed six")

    code, output = run(stub, ["python", "-c", DOWNLOAD_REFUSED])
    check("host outside the list refused", code != 0 and "403" in output, output.splitlines()[-1] if output else "")

    code, output = run(stub, ["python", "-c", IGNORES_PROXY])
    check("request that ignores the proxy fails", code != 0, output.splitlines()[-1] if output else "connected")

    code, output = run(stub, ["python", "-c", LOOKUP], timeout=120)
    check("direct name lookup does not resolve", code != 0, output.splitlines()[-1] if output else "resolved")

    if gateway:
        code, output = run(stub, ["python", "-c", gateway_reachable(gateway)])
        check("gateway reachable", code == 0, f"{gateway}:8080 " + (output.splitlines()[-1] if output else "connected"))

    try:
        custom.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURAL, sandbox_body(second, gateway))
        created.append(second)
        check("second sandbox refused by the quota", False, "it was created")
    except ApiException as error:
        refused = error.status == 403 and "exceeded quota" in (error.body or "")
        check("second sandbox refused by the quota", refused, f"status {error.status}")

    node_name = pod.spec.node_name
    memory = node_memory(node_name)
    if memory:
        allocatable, used = memory
        record("node memory with the sandbox up", f"{node_name}: {allocatable - used:.1f} GiB free of {allocatable:.1f} GiB allocatable ({used:.1f} GiB in use)")
    code, output = run(stub, ["/bin/sh", "-c", "grep -E '^(MemTotal|MemAvailable)' /proc/meminfo; cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/cpu.max 2>&1"])
    record("what the sandbox sees", output.replace("\n", " | "))

    code, output = run(stub, ["python", "-c", CPU_PROBE], timeout=90)
    if code == 0:
        ratio, cpus = output.split()
        binds = float(ratio) < 2.5
        record("cpu limit (2) under gvisor", f"{ratio} CPUs used by 4 busy processes on {cpus} visible: {'binds' if binds else 'does not bind'}")
    else:
        record("cpu limit (2) under gvisor", f"probe failed: {output[-200:]}")

    if memory and memory[0] - memory[1] >= MEMORY_PROBE_MIN_NODE_FREE_GIB:
        try:
            code, output = run(stub, ["python", "-c", MEMORY_PROBE], timeout=120)
            reached = output.strip() if code == 0 else None
        except grpc.RpcError:
            reached = None
        pod = sandbox_pod(name)
        statuses = pod.status.container_statuses or [] if pod else []
        killed = any(
            status.name == "runtime" and (status.last_state.terminated or status.state.terminated)
            and "OOMKilled" in str((status.last_state.terminated or status.state.terminated).reason)
            for status in statuses
        )
        record("memory limit (4Gi) under gvisor", f"allocated {reached or 'less than'} MiB up to {MEMORY_PROBE_MAX_MIB}: {'binds (OOMKilled)' if killed or reached is None else 'does not bind'}")
    else:
        record("memory limit (4Gi) under gvisor", f"probe skipped: the node has less than {MEMORY_PROBE_MIN_NODE_FREE_GIB} GiB free")
finally:
    for sandbox_name in created:
        try:
            custom.delete_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURAL, sandbox_name)
        except ApiException as error:
            if error.status != 404:
                failures.append(f"delete {sandbox_name}")
    gone = wait_for(lambda: not custom.list_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURAL)["items"], 180)
    check("no sandbox left behind", bool(gone), "the quota of one is free")

if failures:
    print(f"FAILED: {', '.join(failures)}", flush=True)
    sys.exit(1)
print("all steps ok", flush=True)
