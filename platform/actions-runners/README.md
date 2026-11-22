# actions-runners

GitHub Actions runners in the cluster. **It is a pilot** (operator decision,
2026-11-22; workspace#279): a week of figures decides whether it stays, widens
or goes. It adds no CPU to the node: it lets a check use memory and cores of
the cluster's VMs that are allotted and idle.

## What runs here

| Namespace | What |
|---|---|
| `actions-runner-controller` | The registrar: one Deployment (`registrar.py`) |
| `actions-runners` | The runner pods the registrar makes, the quota, the network policies, the proof |

The registrar keeps `RUNNERS` runner pods. For a missing one it does what
`infrastructure`'s `github_runner` role does for the CI VMs
(`github-runner-jit.j2`): logs in to Vault with its service account
(Kubernetes auth, role `actions-runners`), has Transit sign the runner App's
JWT (`github-runner-app`), exchanges it for an installation token, asks GitHub
for a just-in-time configuration in the group `0xc0-cluster` with the label
`0xc0-cluster`, and creates the pod. **No key and no credential of GitHub
lives in the cluster**: the App's key never leaves Vault, and a token lives
in the registrar's memory for one pass. GitHub's Actions Runner Controller is
not used because it must hold the App's key itself.

It also deletes a pod that finished, one idle for `IDLE_TIMEOUT_SECONDS`
(an hour) with no job, and removes from GitHub a runner whose pod is gone.
It logs what it does (its logs reach OpenObserve like any pod's) and never a
token, a JWT or a configuration. Its Role in `actions-runners` is `create`,
`get`, `list` and `delete` on pods, and nothing else.

## Who may use it

The runner group `0xc0-cluster` (`.github`) is open to `matchmark` and to the
reusable check alone. A job asks for it with `runs-on: 0xc0-cluster`. Nothing
that logs in to Vault or reaches Proxmox or RustFS runs here: that stays on
the CI VMs.

## The runner pod

GitHub's runner image, pinned by digest to the release the CI VMs run, started
with the just-in-time configuration in an environment variable.

- `runtimeClassName: gvisor`, so it lands on `vm-rke2-04`, the node that has
  `runsc`.
- No service account token, not privileged, no host path, no service links,
  Pod Security `restricted`. What the runner needs that `restricted` does not
  allow: nothing. The root filesystem is writable, because the runner writes
  under its home; its work, tools, cache and `/tmp` are `emptyDir`s with a size
  limit. `allowPrivilegeEscalation: false` means the image's `sudo` does not
  work: nothing in a job may need it.
- The configuration is in the pod's spec. Only the registrar's Role (and the
  cluster's admins) can read a pod; the runner pods have no token at all.
- Requests 1 CPU and 2 Gi, limits 4 CPU and 4 Gi: a check's four test workers
  and its own PostgreSQL ran in 2 vCPU and 4 GB on a CI VM. The worker has 4
  vCPU and 12 GiB, shared with the platform and the task sandboxes, whose
  quota allows 8 Gi of limits: the figures are a proposal until the pilot's
  week measures the worker's free capacity (workspace#279), and the sandboxes
  keep what the runners' quota (two pods, 8 Gi of limits) leaves.
- The PriorityClass `actions-runners` (-200, never preempting) is below the
  platform's and below the sandboxes' `matchmark-batch` (-100): a node short of
  memory evicts a runner first.

### What a check needs

A check installs its tools with mise at run time, runs `git`, starts its own
PostgreSQL (`initdb`, `pg_ctl`) and runs tests that use `bwrap`. GitHub's image
has `git`, `curl` and what mise needs to download, but **not** `bubblewrap`,
`socat` or a compiler, and the pod cannot install them (no root, no
`sudo`). Until the org has an image of its own with them
(0xc0-labs/gitops#583), a check that needs those cannot run here. That image is
its own issue, not built here.

## The network

Default deny in both namespaces (`network-policy.yaml`).

- A runner pod: DNS, and TCP 443 to the internet with `10.0.0.0/8`,
  `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10` and `169.254.0.0/16` left
  out: the pod and service ranges and every network of the org are among them.
  No ingress. The internet's 443 is open to any host: a policy cannot name one.
- The registrar: DNS, Vault on 8200, the Kubernetes API on the three RKE2
  servers, and GitHub's API on 443. No ingress. Vault's own policy
  (`platform/vault/network-policy.yaml`) admits it.

### The proof

`runner-network-proof-1` is a plain Job with the runner's label and shape. It
checks that the API server (by name and by address), Vault, PostgreSQL, the
node's kubelet and SSH, both VIPs, an address in `10.0.0.0/8` and the metadata
address do not answer, and that `https://api.github.com` does.

```sh
kubectl -n actions-runners logs job/runner-network-proof-1
```

It must print `every step passed`. A Job's pod template cannot change: to run
it again, raise the number in its name (`-1` to `-2`) and the sync prunes the
old one.

## Stopping it

`RUNNERS` to `"0"` in `registrar.yaml`: the registrar deletes no running job,
but makes no new pod, and a finished or idle one is not replaced. Or remove the
Application: the `platform` ApplicationSet keeps what it deployed
(`preserveResourcesOnDeletion`), so delete both Namespaces, the
`actions-runners` PriorityClass and the `actions-runners` Vault role by hand.
A runner left in the group is removed from GitHub by hand.

## Upgrading the runner

`RUNNER_IMAGE` in `registrar.yaml`, with `infrastructure`'s
`github_runner_version`: the runner never updates itself and GitHub refuses a
release more than 30 days old. The digest is the image index's.
