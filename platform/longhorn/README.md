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

## The apps taint

The nodes of the `apps` pool (`vm-rke2-04`, `-05`) get the taint
`0xc0.cc/pool=apps:NoSchedule` in a maintenance window (workspace#288). Longhorn
runs there, so it tolerates it twice:

- **The components the chart deploys** (`longhorn-manager`, the driver
  deployer, the UI, the global manager): `global.tolerations` in
  `values.yaml`. Argo CD applies it on sync.
- **The components Longhorn manages itself** (instance managers, CSI plugin,
  engine images, share managers): the `taint-toleration` setting, fed by
  `defaultSettings.taintToleration` in `values.yaml`. Longhorn applies it
  only when no volume is attached ([settings reference][l-settings], [taint
  toleration][l-taint]): with one attached, the setting stays unapplied until
  the next hourly synchronization finds every volume detached, or until it is
  set again.

The sandbox pool's taint is not tolerated: Longhorn stays off `vm-rke2-06`.

### Inside the window

Before the taint lands, with every workload that uses a volume stopped and
every volume detached:

```sh
kubectl -n longhorn-system get volumes.longhorn.io   # every STATE: detached
kubectl -n longhorn-system get settings.longhorn.io taint-toleration \
  -o jsonpath='{.value}{"\n"}{.status.applied}{"\n"}'
```

The value must be `0xc0.cc/pool=apps:NoSchedule` and `status.applied` `true`.
Synced with a volume attached, it reads `false`. Argo CD does not sync it
again by itself, and the docs do not say that a changed Helm value reaches a
setting already created, so set it where the docs do: **Settings, Kubernetes
Taint Toleration in the Longhorn UI**, or
`kubectl -n longhorn-system edit settings.longhorn.io taint-toleration`, with
the same value and with every volume detached. The system-managed components
restart then, and Longhorn is unavailable for a moment. (`helm upgrade`, which
the docs name for the user-deployed components, is not how this repo applies
them: Argo CD does.) Keep `values.yaml` and the setting equal.

Then confirm, before the taint:

```sh
kubectl -n longhorn-system get settings.longhorn.io taint-toleration \
  -o jsonpath='{.status.applied}{"\n"}'
kubectl -n longhorn-system get pods -l longhorn.io/component=instance-manager \
  -o custom-columns=NAME:.metadata.name,TOL:.spec.tolerations
kubectl -n longhorn-system get pods -l app=longhorn-csi-plugin \
  -o custom-columns=NAME:.metadata.name,TOL:.spec.tolerations
```

Every pod lists `0xc0.cc/pool` with value `apps`. Only then does the taint go
on the nodes.

## Recurring trim

A trim hands a volume's freed blocks back to Longhorn; without it a replica
holds every block ever written (a volume that used 2 GB held 12 GiB per
replica, 2026-10-02). `filesystem-trim` (`recurring-jobs.yaml`) is a Longhorn
`RecurringJob` of the group `default`, every six hours at :17, for every volume
with no recurring job of its own.

A large volume that is written all day may take longer than a minute to trim
and show `FailedFilesystemTrim`, `context deadline exceeded`. The recurring
job's API client waits a fixed minute, a constant in
[`app/recurringjob/constant.go`][l-const] that no setting changes
([longhorn/longhorn#6868][l-6868], open again in [#14526][l-14526]); the
manager's `fstrim` goes on after the client gives up, so the space is reclaimed
anyway and the red mark is the client's. The job's retries are what hurt: each
starts another `fstrim` beside the first. A volume like that wants a `CronJob`
of its own that calls `trimFilesystem` on the manager (port 9500, no
credential) and waits for the answer.

Each failed trim is an event, `FailedFilesystemTrim` on the `RecurringJob` in
`longhorn-system`: in Headlamp, or `kubectl -n longhorn-system get events
--field-selector reason=FailedFilesystemTrim`. Grafana alerts on a volume's
bytes and inodes and on a degraded or faulted volume
(`platform/kube-prometheus-stack`, README, Alert rules); none covers the trim.

[l-settings]: https://longhorn.io/docs/1.13.0/references/settings/
[l-taint]: https://longhorn.io/docs/1.13.0/advanced-resources/deploy/taint-toleration/
[l-const]: https://github.com/longhorn/longhorn-manager/blob/v1.13.0/app/recurringjob/constant.go
[l-6868]: https://github.com/longhorn/longhorn/issues/6868
[l-14526]: https://github.com/longhorn/longhorn/issues/14526
