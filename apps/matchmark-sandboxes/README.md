# matchmark-sandboxes

The namespace Matchmark's factory runs its coding agents' sandboxes in, one at
a time, and what surrounds them. The `Sandbox` kind and its controller are
`platform/agent-sandbox`. The factory worker creates them, with the Role in
`worker-role.yaml` (below); the proof creates one too.

It is an Application of its own, `matchmark-sandboxes`, so that a failed sync
here never holds Matchmark's releases back. Its Argo CD project is `apps`,
which now admits the namespace `matchmark-sandboxes` as a destination: nothing
else had to be admitted, since the cluster-scoped kinds it needs
(`PriorityClass`, a read-only `ClusterRole`) are in `platform/agent-sandbox`.

Where a sandbox runs: `vm-rke2-04`, the only node with gVisor, through the
RuntimeClass `gvisor` (`platform/gvisor`).

## The quota

Two `ResourceQuota`s:

| | Hard |
|---|---|
| `sandbox-compute`, scoped to the PriorityClass `matchmark-batch` | 2 pods; requests 2 CPU and 4Gi; limits 4 CPU and 8Gi |
| `sandbox-count` | `count/sandboxes.agents.x-k8s.io: 1` |

The first counts only pods with the sandboxes' priority class, so the proxy
and the proof are not in it. The requests allow two pods' worth of a sandbox
(1 CPU, 2Gi) so that a terminating pod does not block the next. The second is
"one sandbox at a time", refused by the API server on creation. They are two
objects because a scoped quota cannot hold an object count.

## The network

Default deny in both directions, then:

| From | To | Port |
|---|---|---|
| a sandbox | Matchmark's gateway pods (`matchmark`) | 8080 |
| a sandbox | the proxy's pods | 3128 |
| the factory worker's pods (`matchmark`) and the proof | a sandbox | 8080 (files) and 9090 (processes): its runtime, `sandboxd` |
| the proxy | the internet, private ranges left out | 443 |
| the proxy | the cluster's DNS | 53 |
| the proof | the Kubernetes API servers and the sandbox | 6443; 8080 and 9090 |

The factory worker's own egress (the API servers, and the sandboxes' 8080 and
9090) is in `apps/matchmark/prod/network-policy-worker.yaml`.

The gateway's own rule admitting the sandboxes is in `apps/matchmark/prod`
(`network-policy-sandbox.yaml`).

A sandbox has **no DNS**, so the gateway and the proxy are reached by address
(their ClusterIPs). A NetworkPolicy cannot name hosts, Canal has no rule by
domain, and gVisor cannot redirect a pod's traffic, so the tools are pointed
at the proxy by environment (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` for the
gateway, `NODE_USE_ENV_PROXY=1`) and the policy lets the pod reach nothing
else: **a tool that ignores the proxy fails closed**. With DNS, a sandbox
could send data out in the name of a query.

## The proxy

Squid, `egress-proxy`, the sandboxes' only way to the internet.

- It tunnels `CONNECT` to port 443 and never opens TLS. Plain HTTP and other
  ports are refused.
- It allows the hosts in `allowed-hosts.txt` and refuses every other:
  `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`, `github.com`,
  `api.github.com`.
- It refuses private, link-local and cluster addresses on the address a name
  resolves to, whatever the name.
- It accepts connections from pods labelled `matchmark.0xc0.cc/sandbox: "true"`
  alone: the sandboxes, and the proof, which installs its libraries through it.
- Its access log (when, client, method, host, result: `TCP_TUNNEL/200` allowed,
  `TCP_DENIED/403` refused) goes to stdout, from a sidecar following the log
  file (the pod's user cannot open the container's stdout), so the collector
  takes it to OpenObserve as every pod's log. The sidecar drops the lines of
  the readiness probe (a connection with no request,
  `host=error:transaction-end-before-headers`: the kubelet's address changes
  with the node, so no Squid ACL can name it), so the log is the sandboxes'
  alone, and empties each file once it passes 10 MiB, so that the volume
  never grows until the pod is evicted. `cache.log` follows the same way.
- The pinger is off (`pinger_enable off`): the container may not open ICMP
  sockets and there are no peers to measure.

**Changing the allowlist** is a PR here, in this phase: edit
`allowed-hosts.txt`. The ConfigMap is generated with a hash in its name, so
the proxy restarts on its own when the list changes. It will be a setting of
the workspace, in the API, from phase 4 (operator, 2026-11-20).

**A host that is not in the list**: the tunnel is refused with `403`, the tool
fails with that error, and the refusal is in the access log as `TCP_DENIED`.
Nothing else is tried: there is no other way out.

## The proof

`sandbox-proof-4`, a plain Job (`sandbox-proof.yaml`, `proof.py`), kept after it
ends and not run again by a sync. To run it again, raise the number in its
name (`-4` to `-5`): the sync creates the new Job and prunes the old one.

```sh
kubectl -n matchmark-sandboxes logs job/sandbox-proof-4
```

One line per step, `ok` or `FAILED`, and a last line `all steps ok`:

| Step | Shows |
|---|---|
| `gateway address` | the proof reads the `gateway` Service's ClusterIP in `matchmark`; a missing address fails the step |
| `sandbox ready` | a `Sandbox` created as the worker will create one becomes ready |
| `gvisor runtime class`, `kernel is gvisor` | its pod runs under the RuntimeClass `gvisor` |
| `sandboxd rest` | agent-sandbox's runtime answers on 8080, health without a token and metadata with it |
| `call without the token refused` | the same call with no token is refused, over REST (`401`/`403`) and gRPC (`UNAUTHENTICATED`) |
| `command through sandboxd` | a command run over gRPC on 9090, as user `10001`, and its output read back |
| `pip install through the proxy` | one small package installs through the proxy |
| `host outside the list refused` | the proxy answers `403` for a host that is not allowed |
| `request that ignores the proxy fails` | a direct connection to a public address fails |
| `direct name lookup does not resolve` | the sandbox has no DNS |
| `gateway reachable` | the sandbox reaches the gateway by its address on 8080; with no address the step fails |
| `second sandbox refused by the quota` | the API server refuses it: `exceeded quota` |
| `no sandbox left behind` | the Sandboxes it created are gone (each read by name: the worker's proof may hold the quota's one slot at that moment, and that Sandbox is not this proof's) |
| `small sandbox ready`, `no small sandbox left behind` | the second sandbox of the memory probe below, made once the first is gone, runs and is deleted |

The proof makes a random token for each sandbox and passes only its SHA-256 to
`sandboxd` (`--auth-token-sha256`) in the pod's spec, so the token is in no
manifest. Every call but `/v1/health`, which the readiness probe makes, carries
it (`Authorization: Bearer`, or the `authorization` metadata in gRPC). The
NetworkPolicy is the first wall; the token is the second, and the worker's
sandboxes will have it too.

The proof's pod has a read-only root filesystem: `/work` (the libraries'
target and `HOME`) and `/tmp` (`TMPDIR`, for pip's temporaries) are its only
writable paths, and bytecode is not written.

Seven `record` lines are for the first runs to read, not checks:

- the node's free memory with the sandbox up (allocatable less the metrics
  server's working set);
- what the sandbox sees of its memory and CPU;
- whether the CPU and memory limits bind under gVisor (gvisor#13777 and
  #15756 say they may not): four busy processes against a limit of 2 CPUs,
  and an allocation one step past the 4Gi limit, skipped when the node has
  less than 8 GiB free;
- the same for a small memory limit, which needs no free memory: a second
  sandbox with a limit of 256Mi, and a process that allocates up to 512 MiB;
  it records whether the container was killed (`OOMKilled`, its restarts) and
  how the call ended;
- whether a process in the sandbox can make its own namespaces, which
  Claude Code's isolation of its commands (`bubblewrap`) needs: a user
  namespace alone, user, PID and mount namespaces together (with whether
  `/proc/1/environ` is the new process's own), and what the sandbox says
  about user namespaces (`max_user_namespaces`, `unprivileged_userns_clone`).
  Each records the command's exit code and last line, and none fails the proof.

The Job's identity is its own, the ServiceAccount `sandbox-proof`: a Role
limited to the `Sandbox` kind (`create`, `get`, `list`, `delete`) and to
reading pods, in this namespace, plus a read of the node and its metrics
(`platform/agent-sandbox`) and of the `gateway` Service in `matchmark`, by
name (`apps/matchmark/prod/network-policy-sandbox.yaml`).

The sandboxd image is not written here: the Job reads it from the ConfigMap
that `sandboxd-image/` (a component, not an Application) generates, which the
factory worker's Deployment reads too. Bumping it changes the Job's template,
so raise the number in its name then as well.

### The worker's Role

`worker-role.yaml`: what the factory worker's sandbox adapter calls and no
more, for its ServiceAccount in `matchmark`: `create`, `get` and `delete` on
`sandboxes.agents.x-k8s.io` and `get` on `pods`. It has no `list` and no
`watch` (it polls the sandbox it made, by name, and finds its pod's address in
the sandbox's status), and nothing on Secrets: a stolen worker token cannot
enumerate the namespace or read the subscription's token. The sandbox's own
token is derived from a key the worker holds, never stored. The proof here has
more (`list`) because it reads the pods of its sandboxes. What the worker's own
proof shows is in `apps/matchmark/README.md`, Artifacts.

The proof's pod is a sandbox for the network: its label gives it the
sandboxes' egress rules, which is how it installs `k8s-agent-sandbox` and
`kubernetes` from PyPI through the proxy.

## The subscription token

The namespace's **only Secret**: `claude-subscription-vault`, one key,
`oauth_token`, the token of the operator's Claude subscription (from `claude
setup-token`). Vault Secrets Operator makes it from
`apps/matchmark/claude-subscription`, the secret the factory worker reads
too (`apps/matchmark/prod/vault-secrets.yaml`): it is written once, by the
operator, in Vault. The value is trimmed and nothing raw from Vault is kept.

What reads it: a sandbox's pod, by `secretKeyRef` into
`CLAUDE_CODE_OAUTH_TOKEN`, when the coding agent is paid by that
subscription. Nothing mounts it yet: the worker's settings that name it come
with its Role here. The gateway never holds it for a sandbox.

`VaultAuth` uses the Kubernetes auth role `matchmark-sandboxes`
(`0xc0-labs/vault`), bound to this namespace and the ServiceAccount
`vault-secrets`, with read on that one path. The namespace is not labelled
`vault.0xc0.cc/apps`, which would admit it to the shared `apps` role. Nothing
is restarted when the token changes: a sandbox lives for one attempt.

No NetworkPolicy is added: Vault Secrets Operator, in its own namespace,
talks to Vault and writes the Secret through the API server, and no pod here
does either.

The risk this accepts, and why: `docs/accepted-risks.md` in
[`0xc0-labs/workspace`](https://github.com/0xc0-labs/workspace/blob/main/docs/accepted-risks.md),
and Matchmark's design, `docs/architecture/sandbox.md` in
[`0xc0-labs/matchmark`](https://github.com/0xc0-labs/matchmark/blob/main/docs/architecture/sandbox.md).

## The pod's shape

What the proof's sandbox has and the worker's will: user `10001`,
`allowPrivilegeEscalation: false`, every capability dropped, `RuntimeDefault`
seccomp, a read-only root filesystem, no ServiceAccount token, no service
links, requests of 1 CPU and 2Gi, limits of 2 CPU and 4Gi, ephemeral storage
of 10Gi, size-limited `emptyDir`s for `/workspace`, `/tmp` and the caches, and
`sandboxd` copied in by an init container from the `sandboxd` image.

## Removing it

Delete `apps/matchmark-sandboxes/`. The `apps` ApplicationSet keeps what the
Application deployed when it goes: delete the namespace by hand afterwards.
