# gvisor

The cluster's `gvisor` RuntimeClass (`handler: runsc`) and two Jobs that show
what running under gVisor is worth. **It is a test** (operator decision,
2026-11-20; workspace#266), not a service: Matchmark's agent sandboxes are
a later phase.

`runsc` exists on `vm-rke2-04` alone (infrastructure#268), so the class's
`scheduling.nodeSelector` sends every pod that asks for it there.

## The two Jobs

Both run the same image and the same workload (`workload.py`, a ConfigMap),
with no network, no ServiceAccount token, a read-only root filesystem and an
`emptyDir` to work in. The workload is what a coding agent's sandbox does,
in small: 3000 small files written and read, 150 processes started, 300
modules byte-compiled.

| Job | Runtime |
|---|---|
| `gvisor-proof` | `gvisor` |
| `runc-baseline` | the default, on the same node |

Each prints which runtime it believes it runs in, checked from the kernel the
pod sees (gVisor's is a fixed `4.4.0`; the node's is not), and **fails** when
that is not the runtime it expected.

## Reading the logs

```sh
kubectl -n gvisor logs job/gvisor-proof
kubectl -n gvisor logs job/runc-baseline
```

`gvisor-proof` must print `inside gVisor: True`, `runc-baseline` `False`. Each
prints the time of its three phases and a `total:`; the difference between the
two totals is the cost of the sandbox for that kind of work.

The Jobs are plain, not sync hooks, and are kept after they finish. A sync
does not run them again; to run them again, delete them and Argo CD recreates
them.

## Removing it

Delete `platform/gvisor/`. The `platform` ApplicationSet keeps what an
Application deployed when it goes (`preserveResourcesOnDeletion`), so delete
the `gvisor` Namespace and the `gvisor` RuntimeClass by hand afterwards.
