# agent-sandbox

[agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox) `v1.0.11`:
the `Sandbox` kind (`agents.x-k8s.io/v1beta1`) and the controller that turns
one into a pod. Matchmark's factory will run each coding agent in one
(`apps/matchmark-sandboxes`). Nothing uses it yet.

## What is installed

The release's `sandbox.yaml`, the core alone, referenced by its tag in
`kustomization.yaml`. The release's extensions (templates, pools, claims) are
not used, so they are not installed. Its controller image is pinned by digest.

What the release leaves out and this folder adds:

- **The controller's resources**: requests 10m and 64Mi, limits 500m and
  256Mi (the chart's example, with more memory).
- **Pod Security `restricted`** on `agent-sandbox-system`, and a security
  context for the controller to pass it.
- **A default deny** in that namespace, and the controller's way to the
  Kubernetes API.
- **The PriorityClass `matchmark-batch`** the sandboxes use (value -100,
  `preemptionPolicy: Never`): it is cluster-scoped, which the `apps` project
  does not admit, so it is here.
- **A read-only ClusterRole for the sandbox proof** (`proof-node-reader.yaml`):
  the node and its metrics, to record free memory. What creates a `Sandbox`
  is the proof's own Role, in its namespace.

The component sets `Prune=false`: it holds CRDs, and deleting them deletes
every `Sandbox`.

## Upgrading

Change the tag in `kustomization.yaml`'s resource URL and in `images:` (the
digest: `crane digest registry.k8s.io/agent-sandbox/agent-sandbox-controller:<tag>`),
and `sandboxd`'s tag and digest in `apps/matchmark-sandboxes`.
