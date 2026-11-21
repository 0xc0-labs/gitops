# Longhorn

Block storage for the cluster's volumes, three replicas each, on the data disks
`infrastructure`'s `longhorn_node` joins into `/var/lib/longhorn`.

## Recurring trims

`recurring-jobs.yaml` holds two `filesystem-trim` jobs. A trim hands a
volume's freed blocks back to Longhorn; without it a replica holds every block
ever written.

| Job | Group | Runs | Volumes |
|---|---|---|---|
| `filesystem-trim` | `default` | every six hours, at :17 | every volume with no recurring job of its own |
| `filesystem-trim-openobserve` | `openobserve` | every hour, at :47 | OpenObserve's |

### A trim that times out

Seen on 2026-11-21 (#521): `FailedFilesystemTrim` on OpenObserve's volume,
`Post "http://longhorn-backend:9500/v1/volumes/<pv>?action=trimFilesystem":
context deadline exceeded (Client.Timeout exceeded while awaiting headers)`,
and, on one run, the whole job timed out.

The recurring job calls that endpoint with a client that waits one minute, and
the wait is not configurable ([longhorn/longhorn#6868][l-6868]; the same
error is open again in [#14526][l-14526]). The trim itself goes on in the node, where
Longhorn lets `fstrim` run for an hour (longhorn-manager#2982), so a retry
finds it done or shorter.
OpenObserve writes all the time and its volume held about 34 GB: six hours of
freed blocks took longer than the minute. Every other volume trims in about
five seconds. The job also runs one volume at a time, so the slow one most likely
holds the run's pod past its deadline (the 00:17 run, whose pod is gone, could
not be read).

The fix is not a longer wait, which Longhorn does not offer, but a smaller
trim: OpenObserve's volume has a job of its own, every hour, at an hour away
from the others.

### Putting a volume in the group

A volume takes the `default` group only while it has none of its own. The
group of another is set by the claim, which Longhorn syncs to the volume when
the claim says it is the source. OpenObserve's claim is made by the
StatefulSet's `volumeClaimTemplates`, which cannot change once it exists, so
the existing claim gets its labels by hand, once:

```sh
kubectl -n openobserve label pvc data-openobserve-0 \
  recurring-job.longhorn.io/source=enabled \
  recurring-job-group.longhorn.io/openobserve=enabled
```

Within a few minutes the volume carries `recurring-job-group.longhorn.io/openobserve`
and no longer `.../default`:

```sh
kubectl -n longhorn-system get volume -l recurring-job-group.longhorn.io/openobserve=enabled
```

A claim created later (the volume restored or replaced) needs the same two
labels. Until it has them, the default job trims it, as before.

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

[l-6868]: https://github.com/longhorn/longhorn/issues/6868
[l-14526]: https://github.com/longhorn/longhorn/issues/14526
