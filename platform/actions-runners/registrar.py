"""Keeps RUNNERS just-in-time GitHub Actions runner pods in the cluster (README.md).

Does what infrastructure's `github-runner-jit` step does for the CI VMs: Vault
signs the runner App's JWT with Transit, so the App's key never comes here;
the JWT buys an installation token, and the token buys a just-in-time runner
configuration, which goes to a fresh pod as an environment variable.

Standard library only. Logs what it does and never a token, a JWT or a
configuration: errors carry the HTTP status, not the response.
"""

import base64
import calendar
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid

RUNNERS = int(os.environ["RUNNERS"])
NAMESPACE = os.environ["RUNNER_NAMESPACE"]
IMAGE = os.environ["RUNNER_IMAGE"]
GROUP = os.environ["RUNNER_GROUP"]
LABEL = os.environ["RUNNER_LABEL"]
PRIORITY_CLASS = os.environ["RUNNER_PRIORITY_CLASS"]
IDLE_TIMEOUT = int(os.environ["IDLE_TIMEOUT_SECONDS"])
INTERVAL = int(os.environ.get("INTERVAL_SECONDS", "15"))
# A runner is registered a moment before its pod exists: the pod's absence
# means nothing until this long after.
REGISTRATION_GRACE = 180
SWEEP_SECONDS = 300
APP_ID = os.environ["GITHUB_APP_ID"]
INSTALLATION_ID = os.environ["GITHUB_APP_INSTALLATION_ID"]
VAULT_ADDR = os.environ["VAULT_ADDR"]
VAULT_ROLE = os.environ["VAULT_ROLE"]
TRANSIT_KEY = os.environ["VAULT_TRANSIT_KEY"]
GITHUB_API = "https://api.github.com"
KUBE_API = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT']}"
KUBE_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
VAULT_TOKEN_FILE = "/var/run/secrets/vault/token"
HEALTH_FILE = "/tmp/alive"
PREFIX = "cluster-runner-"
# The runners' label alone also matches the proof Job's pod, which carries it
# for the network policy: the registrar's own mark is what makes a pod its.
POD_LABELS = "app.kubernetes.io/name=actions-runner,app.kubernetes.io/managed-by=actions-runner-registrar"

for url in (VAULT_ADDR, GITHUB_API, KUBE_API):
    if not url.startswith(("http://", "https://")):
        sys.exit("every address must be an http(s) URL")

KUBE_TLS = ssl.create_default_context(cafile=f"{KUBE_DIR}/ca.crt")
# Runners registered by this process a moment ago, by name.
registered_at = {}
last_sweep = 0.0


def log(message):
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message, flush=True)


def read(path):
    with open(path) as f:
        return f.read().strip()


def request(method, url, headers, body=None, context=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=30, context=context) as r:  # nosemgrep: dynamic-urllib-use-detected - every address is checked as http(s) at the top of this file
        raw = r.read()
        return json.loads(raw) if raw else None


def kube(method, path, body=None):
    # The projected token rotates: read it on every call.
    return request(method, KUBE_API + path, {"Authorization": "Bearer " + read(f"{KUBE_DIR}/token")}, body, KUBE_TLS)


def github(method, path, token, body=None):
    return request(method, GITHUB_API + path, {
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"}, body)


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def app_jwt():
    """The App's JWT, signed by Vault: the key never comes here."""
    login = request("POST", f"{VAULT_ADDR}/v1/auth/kubernetes/login", {},
                    {"role": VAULT_ROLE, "jwt": read(VAULT_TOKEN_FILE)})
    vault_token = login["auth"]["client_token"]
    now = int(time.time())
    signing_input = (
        b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + "." +
        b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": APP_ID}).encode()))
    # PKCS#1 v1.5 over SHA-256 is RS256. Transit answers vault:v<n>:<base64>.
    signed = request("POST", f"{VAULT_ADDR}/v1/transit/sign/{TRANSIT_KEY}/sha2-256",
                     {"X-Vault-Token": vault_token},
                     {"input": base64.b64encode(signing_input.encode()).decode(),
                      "signature_algorithm": "pkcs1v15"})
    signature = base64.b64decode(signed["data"]["signature"].split(":")[2])
    return signing_input + "." + b64url(signature)


def installation():
    """The organization the App is installed on, by its current name, and a token for it."""
    jwt = app_jwt()
    org = github("GET", f"/app/installations/{INSTALLATION_ID}", jwt)["account"]["login"]
    return org, github("POST", f"/app/installations/{INSTALLATION_ID}/access_tokens", jwt)["token"]


def group_id(org, token):
    groups = github("GET", f"/orgs/{org}/actions/runner-groups?per_page=100", token)["runner_groups"]
    for group in groups:
        if group["name"] == GROUP:
            return group["id"]
    raise RuntimeError(f"runner group {GROUP} not found")


def runner_pod(name, jit_config):
    """The pod spec; the README says why each field is what it is."""
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": {
            "app.kubernetes.io/name": "actions-runner",
            "app.kubernetes.io/managed-by": "actions-runner-registrar"}},
        "spec": {
            "runtimeClassName": "gvisor",
            "priorityClassName": PRIORITY_CLASS,
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "terminationGracePeriodSeconds": 30,
            "securityContext": {
                "runAsNonRoot": True, "runAsUser": 1001, "runAsGroup": 1001, "fsGroup": 1001,
                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{
                "name": "runner",
                "image": IMAGE,
                "command": ["/home/runner/run.sh"],
                "env": [{"name": "ACTIONS_RUNNER_INPUT_JITCONFIG", "value": jit_config}],
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]}},
                "resources": {
                    "requests": {"cpu": os.environ["RUNNER_CPU_REQUEST"], "memory": os.environ["RUNNER_MEMORY_REQUEST"],
                                 "ephemeral-storage": "2Gi"},
                    "limits": {"cpu": os.environ["RUNNER_CPU_LIMIT"], "memory": os.environ["RUNNER_MEMORY_LIMIT"],
                               "ephemeral-storage": "10Gi"}},
                "volumeMounts": [
                    {"name": "work", "mountPath": "/home/runner/_work"},
                    {"name": "tools", "mountPath": "/home/runner/.local"},
                    {"name": "cache", "mountPath": "/home/runner/.cache"},
                    {"name": "tmp", "mountPath": "/tmp"}]}],
            "volumes": [
                {"name": "work", "emptyDir": {"sizeLimit": "6Gi"}},
                {"name": "tools", "emptyDir": {"sizeLimit": "2Gi"}},
                {"name": "cache", "emptyDir": {"sizeLimit": "2Gi"}},
                {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}]}}


def owned(pod):
    metadata = pod["metadata"]
    return (metadata.get("labels", {}).get("app.kubernetes.io/managed-by") == "actions-runner-registrar"
            and metadata["name"].startswith(PREFIX))


def list_pods():
    # The selector narrows the list; `owned` is the rule, checked again here.
    items = kube("GET", f"/api/v1/namespaces/{NAMESPACE}/pods?labelSelector={POD_LABELS}")["items"]
    return [pod for pod in items if owned(pod)]


def delete_pod(name, why):
    log(f"deleting pod {name}: {why}")
    try:
        kube("DELETE", f"/api/v1/namespaces/{NAMESPACE}/pods/{name}")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise


def age_seconds(pod):
    created = calendar.timegm(time.strptime(pod["metadata"]["creationTimestamp"], "%Y-%m-%dT%H:%M:%SZ"))
    return time.time() - created


def reconcile():
    pods = list_pods()
    live = []
    for pod in pods:
        if pod["status"].get("phase") in ("Succeeded", "Failed"):
            delete_pod(pod["metadata"]["name"], pod["status"]["phase"].lower())
        else:
            live.append(pod)
    missing = RUNNERS - len(live)
    stale_candidates = [p for p in live if age_seconds(p) > IDLE_TIMEOUT]
    global last_sweep
    # GitHub is asked only when there is something to do, and every
    # SWEEP_SECONDS to remove the registrations of pods that are gone.
    if missing <= 0 and not stale_candidates and time.time() - last_sweep < SWEEP_SECONDS:
        return
    last_sweep = time.time()
    org, token = installation()
    gid = group_id(org, token)
    runners = github("GET", f"/orgs/{org}/actions/runner-groups/{gid}/runners?per_page=100", token)["runners"]
    by_name = {r["name"]: r for r in runners}

    # A pod that never took a job in IDLE_TIMEOUT is replaced; one that has a
    # job is left to finish.
    for pod in stale_candidates:
        name = pod["metadata"]["name"]
        runner = by_name.get(name)
        if runner is not None and not runner["busy"]:
            delete_pod(name, f"idle for more than {IDLE_TIMEOUT} s")
            live.remove(pod)
            missing += 1

    pod_names = {p["metadata"]["name"] for p in pods}
    for name, runner in by_name.items():
        if not name.startswith(PREFIX) or name in pod_names:
            continue
        if time.time() - registered_at.get(name, 0) < REGISTRATION_GRACE:
            continue
        log(f"removing runner {name} from GitHub: its pod is gone")
        try:
            github("DELETE", f"/orgs/{org}/actions/runners/{runner['id']}", token)
        except urllib.error.HTTPError as e:
            log(f"could not remove runner {name}: HTTP {e.code}")
    for name in [n for n, t in registered_at.items() if time.time() - t > REGISTRATION_GRACE]:
        del registered_at[name]

    for _ in range(max(missing, 0)):
        name = PREFIX + uuid.uuid4().hex[:10]
        config = github("POST", f"/orgs/{org}/actions/runners/generate-jitconfig", token, {
            "name": name, "runner_group_id": gid, "labels": [LABEL], "work_folder": "_work"})
        registered_at[name] = time.time()
        try:
            kube("POST", f"/api/v1/namespaces/{NAMESPACE}/pods", runner_pod(name, config["encoded_jit_config"]))
        except urllib.error.HTTPError as e:
            log(f"could not create pod {name}: HTTP {e.code}; removing its registration")
            runner_id = config["runner"]["id"]
            github("DELETE", f"/orgs/{org}/actions/runners/{runner_id}", token)
            del registered_at[name]
            continue
        log(f"created pod {name}, registered in group {GROUP} with label {LABEL}")


def main():
    log(f"registrar up: {RUNNERS} runner(s) in {NAMESPACE}, group {GROUP}")
    while True:
        try:
            reconcile()
        except urllib.error.HTTPError as e:
            log(f"reconcile failed: HTTP {e.code} from {e.url.split('?')[0].split('/')[2]}")
        except Exception as e:  # noqa: BLE001 - one bad pass must not end the loop
            log(f"reconcile failed: {type(e).__name__}")
        with open(HEALTH_FILE, "w") as f:
            f.write(str(time.time()))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
