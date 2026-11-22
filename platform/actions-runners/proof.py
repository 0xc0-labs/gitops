"""Proof that a runner pod reaches the internet and nothing of the cluster (README.md, The proof).

Runs as a runner-like pod, so the runner's network policy applies to it. Each
target of the cluster or the org's networks must refuse or time out; GitHub's
API must answer. Prints one line per step and exits non-zero when one fails.
"""

import socket
import ssl
import sys
import urllib.request

TIMEOUT = 4
failures = []


def check(step, ok, detail=""):
    print(f"{step}: {'ok' if ok else 'FAILED'} ({detail})", flush=True)
    if not ok:
        failures.append(step)


def reaches(host, port):
    """True when a TCP connection opens, False when it is refused or times out."""
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return True, "connected"
    except socket.gaierror as e:
        # The name does not resolve: nothing was reached, but the step proves
        # nothing about the network, so say so.
        return False, f"does not resolve: {e}"
    except OSError as e:
        return False, type(e).__name__


# Names (DNS answers) and addresses, so the policy is tested on its own and
# not through what DNS happens to return.
BLOCKED = [
    ("the Kubernetes API, by its service name", "kubernetes.default.svc.cluster.local", 443),
    ("the Kubernetes API, on an RKE2 server", "10.10.4.21", 6443),
    ("Vault", "vault-active.vault.svc.cluster.local", 8200),
    ("PostgreSQL", "postgres.postgres.svc.cluster.local", 5432),
    ("the node's kubelet", "10.10.4.24", 10250),
    ("the node's SSH", "10.10.4.24", 22),
    ("the cluster's VIP", "10.10.4.10", 443),
    ("the internal VIP", "10.10.4.9", 443),
    ("an address in 10.0.0.0/8", "10.0.0.1", 443),
    ("the cloud metadata address", "169.254.169.254", 80),
]

for step, host, port in BLOCKED:
    opened, detail = reaches(host, port)
    check(f"{step} does not answer", not opened, f"{host}:{port}, {detail}")

try:
    with urllib.request.urlopen("https://api.github.com/zen", timeout=10, context=ssl.create_default_context()) as r:  # nosemgrep: dynamic-urllib-use-detected - a fixed https URL
        check("api.github.com answers", r.status == 200, f"status {r.status}")
except OSError as e:
    check("api.github.com answers", False, type(e).__name__)

if failures:
    sys.exit(f"{len(failures)} step(s) failed: {', '.join(failures)}")
print("every step passed")
