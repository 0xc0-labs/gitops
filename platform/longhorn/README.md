# Longhorn

Block storage for the cluster's volumes, two replicas each, on the data disks
`infrastructure`'s `longhorn_node` joins into `/var/lib/longhorn`.

## Two replicas

Operator decision, 2026-11-25 (workspace#285): `defaultSettings.defaultReplicaCount`
and `persistence.defaultClassReplicaCount` in `values.yaml` are both 2.

- **Why two.** Every Longhorn node is a VM on one host, on RAID 1. A third
  replica would not survive anything the second does not: replicas cover a VM
  that reboots or breaks, and PBS covers the host.
- **What two does not cover.** While a node reboots, the volumes with a
  replica on it run on one copy until it is rebuilt. A second failure in that
  window, of the node holding the other copy, makes the volume unavailable
  until one of the two nodes is back; it is lost only if both disks are, and
  then PBS restores it.
- **Replicas go on different nodes** (`replica-soft-anti-affinity` is false),
  so a volume needs two schedulable nodes with room.

### Moving to two, once, after the merge

Neither the setting nor the StorageClass reaches what exists, and the
operator runs these steps by hand, in this order.

**1. The default StorageClass.** The chart renders its parameters inside the
`longhorn-storageclass` ConfigMap, not as a `StorageClass`, so Argo CD never
replaces the existing one, and its parameters cannot change in place. Delete
it and create it again from the ConfigMap. Volumes already bound keep
working: a PV carries its own attributes and does not read the class again.

```sh
kubectl get storageclass longhorn -o jsonpath='{.parameters.numberOfReplicas}{"\n"}'   # 3
kubectl delete storageclass longhorn
kubectl -n longhorn-system get configmap longhorn-storageclass \
  -o jsonpath='{.data.storageclass\.yaml}' | kubectl apply -f -
kubectl get storageclass longhorn -o jsonpath='{.parameters.numberOfReplicas}{"\n"}'   # 2
```

**2. The existing volumes.** One loop, which stops at the first error.
Longhorn does not remove the surplus replica by itself (measured on
2026-11-25, Longhorn 1.13.0: the loop's wait for two replicas timed out on the
first volume), so for each attached volume the loop deletes one replica, the
one on the node with the most storage scheduled, and waits until the volume is
`healthy` with two replicas. At no point are two volumes rebuilding at once. A
detached one (`status.state` is `detached`) reports `unknown` robustness and
would never pass that wait: it is patched and the loop goes on without
waiting.

```sh
set -e
node=$(kubectl -n longhorn-system get nodes.longhorn.io -o json | jq -r '
  [.items[] | {n: .metadata.name, s: ([.status.diskStatus[].storageScheduled] | add)}]
  | max_by(.s).n')
for v in $(kubectl -n longhorn-system get volumes.longhorn.io -o name); do
  name=${v#*/}
  kubectl -n longhorn-system patch "$v" --type merge -p '{"spec":{"numberOfReplicas":2}}'
  if [ "$(kubectl -n longhorn-system get "$v" -o jsonpath='{.status.state}')" = detached ]; then
    echo "$v: detached, patched, not waited on"
    continue
  fi
  replicas=$(kubectl -n longhorn-system get replicas.longhorn.io -l "longhornvolume=$name" -o json)
  if [ "$(echo "$replicas" | jq '.items | length')" -gt 2 ]; then
    kubectl -n longhorn-system delete replicas.longhorn.io "$(echo "$replicas" \
      | jq -r --arg n "$node" '[.items[] | select(.spec.nodeID == $n)][0].metadata.name // .items[0].metadata.name')"
  fi
  until [ "$(kubectl -n longhorn-system get "$v" -o jsonpath='{.status.robustness}')" = healthy ] \
    && [ "$(kubectl -n longhorn-system get replicas.longhorn.io \
         -l "longhornvolume=$name" -o name | wc -l | tr -d ' ')" = 2 ]; do
    sleep 5
  done
  echo "$v: healthy, 2 replicas"
done
```

A detached volume (`mariadb-dumps` and `postgres-dumps` on 2026-11-25) keeps
its third replica: once it is attached, run the loop again. The loop deletes a
replica only from a volume that has three, so it is safe to repeat.

**3. Check.** Read-only. Every volume has `2` in its spec, and every attached
one is `healthy` with no replica left over:

```sh
kubectl -n longhorn-system get volumes.longhorn.io \
  -o custom-columns=NAME:.metadata.name,SPEC:.spec.numberOfReplicas,STATE:.status.state,ROBUSTNESS:.status.robustness
kubectl -n longhorn-system get replicas.longhorn.io \
  -o custom-columns=VOLUME:.spec.volumeName --no-headers | sort | uniq -c
```

The last command counts replicas per volume: every attached volume says `2`.
A detached one still says `3` until it is attached.

## Recurring trims

A trim hands a volume's freed blocks back to Longhorn; without it a replica
holds every block ever written.

| Job | Kind | Runs | Volumes |
|---|---|---|---|
| `filesystem-trim` (`recurring-jobs.yaml`) | Longhorn `RecurringJob`, group `default` | every six hours, at :17 | every volume with no recurring job of its own |
| `filesystem-trim-openobserve` (`trim-openobserve.yaml`) | `CronJob` | every hour, at :47 | OpenObserve's |

### A trim that outlasts the recurring job

Seen on 2026-11-21 (#521) and again on 2026-11-22 (#575):
`FailedFilesystemTrim` on OpenObserve's volume, `Post
"http://longhorn-backend:9500/v1/volumes/<pv>?action=trimFilesystem": context
deadline exceeded (Client.Timeout exceeded while awaiting headers)`. Every
other volume trims in about five seconds; OpenObserve's (50 GiB, written all
day) takes longer than a minute even when trimmed hourly (#522).

What Longhorn 1.13.0 does, from its source (`longhorn-manager` v1.13.0):

- **The wait is fixed.** The recurring job's API client is built with
  `HTTPClientTimout = 1 * time.Minute`, a constant in
  [`app/recurringjob/constant.go`][l-const]; no setting, `RecurringJob` field
  or environment variable changes it ([longhorn/longhorn#6868][l-6868], open
  again in [#14526][l-14526]).
- **The trim goes on after the client gives up.** The manager's `trimFilesystem`
  handler runs `fstrim` on the volume's node with a one-hour limit, and takes
  no request context ([`util.TrimFilesystem`][l-util]); the manager serves with
  `http.ListenAndServe` and a proxy with no timeout of their own, so a client
  that waits longer is not cut off, and one that leaves (the `502` in the
  manager's log) leaves `fstrim` running. The space is reclaimed anyway: the
  job's red mark is the client's, not the trim's. The job's retries are what
  hurt: each starts another `fstrim` beside the first, and answers `500`.
- **What the docs offer** ([Trim Filesystem][l-doc]): the UI button, `fstrim`
  by hand, a `RecurringJob`, and the `discard` mount option, which the page
  itself says to use with caution, since it triggers snapshot removal often
  and interrupts operations such as backup creation. It is a StorageClass
  `mountOptions` entry, applied when a volume is provisioned: OpenObserve's
  claim cannot change (a StatefulSet's `volumeClaimTemplates`), so it would
  mean replacing the volume and its data.

Chosen: a `CronJob` of ours that calls the same action and waits for the
answer (`curl --max-time 3700`, a little over the node's hour), and replaces
the volume's `RecurringJob`. It has no service account token, nothing in
Vault, and no credential at all: Longhorn's API takes none. Its
NetworkPolicy, `trim-openobserve-to-manager`, opens the manager's port 9500
to its pod alone. Not retried inside a run (`backoffLimit: 0`) and never two at
once (`concurrencyPolicy: Forbid`): the next hour's run is the retry. What it
does not do is the recurring job's last step, purging snapshots a trim marked
as removed, which only happens when *Remove Snapshots During Filesystem Trim*
or the volume's `unmapMarkSnapChainRemoved` asks for it; OpenObserve's volume
is `ignored` and the setting off.

The job names the volume (`pvc-96a82cef-…`). A replaced volume has a new name,
and the job answers `404` until it is changed here.

### Replacing the recurring job

Argo CD prunes nothing in this component, so the merge does not delete the old
`RecurringJob`; the operator does, once, after the sync:

```sh
kubectl -n longhorn-system delete recurringjob filesystem-trim-openobserve
```

The claim keeps its `recurring-job-group.longhorn.io/openobserve` label (and
`recurring-job.longhorn.io/source`), on purpose. A volume takes the `default`
group only while it has no group of its own, so the label keeps the six-hourly
job away from it, and the group no longer holds a job. Removing the label
would give the volume back to the default job, which fails on it again. A
claim created later (the volume restored or replaced) needs the same two
labels, set by hand because the StatefulSet's claim template cannot change:

```sh
kubectl -n openobserve label pvc data-openobserve-0 \
  recurring-job.longhorn.io/source=enabled \
  recurring-job-group.longhorn.io/openobserve=enabled
```

### Telling that it works

Read-only, from the cluster:

```sh
# The job's result: every hour a Job that Completed, none Failed.
kubectl -n longhorn-system get job -l app.kubernetes.io/name=filesystem-trim-openobserve
kubectl -n longhorn-system logs job/<job>   # "trimFilesystem answered HTTP 200 after Ns"

# The old job is gone and nothing else trims this volume.
kubectl -n longhorn-system get recurringjob
kubectl -n longhorn-system get events --field-selector reason=FailedFilesystemTrim

# The volume's size: actual (Longhorn's count) against what the filesystem uses.
kubectl -n longhorn-system get volume pvc-96a82cef-3be6-4ad2-afba-b8172b092c73 \
  -o jsonpath='{.status.actualSize}{"\n"}'
```

Read `actualSize` before the first run and after it: after a trim it falls
toward the filesystem's used space (OpenObserve's own figure, in its README,
Retention and size), and on the Longhorn dashboard of OpenObserve
`longhorn_volume_actual_size_bytes` should stop climbing past it between
runs. The first run after the change may be the slowest, and its pod may
outlive a minute: that is the point.

### Seeing a failure

- Each failed trim is an event, `FailedFilesystemTrim` on the `RecurringJob`
  in `longhorn-system`: in Headlamp, with `kubectl -n longhorn-system get
  events --field-selector reason=FailedFilesystemTrim`, and in OpenObserve's
  `k8s_events` stream, which `openobserve-collector` fills.
- The job pods say which volume: `kubectl -n longhorn-system logs <pod>`.
  Longhorn keeps only the last failed and the last successful job of each
  kind (history limits of 1), so read them soon.
- A volume that is not trimmed shows as `longhorn_volume_actual_size_bytes`
  growing past its used space, on the Longhorn dashboard of OpenObserve.
- No alert covers any of it: OpenObserve has none (its README, Alerts), and
  none is built here.

[l-const]: https://github.com/longhorn/longhorn-manager/blob/v1.13.0/app/recurringjob/constant.go
[l-util]: https://github.com/longhorn/longhorn-manager/blob/v1.13.0/util/util.go
[l-doc]: https://longhorn.io/docs/1.13.0/nodes-and-volumes/volumes/trim-filesystem/
[l-6868]: https://github.com/longhorn/longhorn/issues/6868
[l-14526]: https://github.com/longhorn/longhorn/issues/14526
