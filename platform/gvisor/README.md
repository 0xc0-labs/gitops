# gvisor

The cluster's `gvisor` RuntimeClass (`handler: runsc`) and two Jobs that show
what running under gVisor is worth. **It is a test** (operator decision,
2026-11-20; workspace#266), not a service: Matchmark's agent sandboxes are
a later phase.

`runsc` exists on the `sandbox` pool alone (`vm-rke2-06`, tainted
`0xc0.cc/pool=sandbox:NoSchedule`), so the class's `scheduling` sends every pod
that asks for it there, with the toleration: a workload sets `runtimeClassName`
and nothing else. A pod that also sets a `nodeSelector` of another pool is
refused at admission.

## The two Jobs

Both run the same image and the same workload (`workload.py`, a ConfigMap),
with no network, no ServiceAccount token, a read-only root filesystem and an
`emptyDir` to work in. The workload is what a coding agent's sandbox does,
in small: 3000 small files written and read, 150 processes started, 300
modules byte-compiled.

| Job | Runtime |
|---|---|
| `gvisor-proof-3` | `gvisor` |
| `runc-baseline-3` | the default, pinned to the same pool |

Each prints which runtime it believes it runs in, checked from the kernel the
pod sees (gVisor's release names it, as `4.19.0-gvisor`; the node's does not), and **fails** when
that is not the runtime it expected.

## Reading the logs

```sh
kubectl -n gvisor logs job/gvisor-proof-3
kubectl -n gvisor logs job/runc-baseline-3
```

`gvisor-proof-3` must print `inside gVisor: True`, `runc-baseline-3` `False`. Each
prints the time of its three phases and a `total:`; the difference between the
two totals is the cost of the sandbox for that kind of work.

The Jobs are plain, not sync hooks, and are kept after they finish. A sync
does not run a finished Job again and a Job's pod template cannot change, so
to run them again, raise the number in both names (`-3` to `-4`): the sync
creates the new pair and prunes the old one. Both always carry the same
number, so the pair comes from the same workload.

## Removing it

Delete `platform/gvisor/`. The `platform` ApplicationSet keeps what an
Application deployed when it goes (`preserveResourcesOnDeletion`), so delete
the `gvisor` Namespace and the `gvisor` RuntimeClass by hand afterwards.
