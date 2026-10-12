# OpenObserve

Metrics, logs and traces of the cluster in one backend, at
`https://o2.int.0xc0.cc` (WARP only). Single node, local disk on a Longhorn
volume, 30 days of retention. The collectors in `openobserve-collector` send to
it; what they collect is in their `kustomization.yaml`.

## Credentials: the root user and the collectors' service account

Two, both from Vault through Vault Secrets Operator, written as [`vault`'s
README, Writing or rotating a secret][vault-write] says:

| What | Vault | Read by |
|---|---|---|
| The root user, the operator's login | `platform/openobserve/root` (`email`, `password`) | OpenObserve, which creates the root from it on its first start |
| The collectors' service account | `platform/openobserve-collector/ingest` (`email`, `token`) | the collectors, as basic auth |

**The root user.** OpenObserve creates it from the environment once, on its
first start, and never updates it from there (seen on the first sync,
2026-10-01). Changing its password is therefore two steps, in this order:
first in OpenObserve (the UI, user settings), then the same value in Vault
with `kv patch`. Changing its email means recreating the volume. To log in,
read the password when needed:
`vault kv get -mount=platform -field=password openobserve/root`.

**The service account** is made in OpenObserve (IAM, then Service accounts),
and OpenObserve generates its token, shown once: `token` at
`platform/openobserve-collector/ingest`, beside `email`, the account's
address (`collector@0xc0.cc`), which is not a secret.

Rotating it is a new token in OpenObserve, written with `kv patch`, then the
old token revoked there. Vault Secrets Operator rewrites the collectors' Secret
within the hour and restarts them. Changing the root user's password does not
touch ingestion.

## Dashboards: from git

Every dashboard is a JSON file in `dashboards/`, imported into the `0xc0`
folder by a PostSync Job after every sync (`dashboards.yaml`,
`dashboards/import.py`): updated by title, created when new, and deleted from
the folder when its file is gone. Git wins: an edit made in the UI to a
dashboard in `0xc0` is overwritten on the next sync. Dashboards in other
folders are left alone, so the UI stays free for trying things.

- **From the community repository**
  ([openobserve/dashboards](https://github.com/openobserve/dashboards)): the
  Kubernetes ones built for this collector (overview, nodes, node pressure,
  namespaces, pods, events), host metrics, ArgoCD and traces. Node pressure is
  rewritten on kube-state-metrics' `kube_node_status_condition`: its own
  queries need the collector's k8s_cluster receiver, which this chart does not
  run. Left out, because they do not work here:
  - the API server and "Compute Resources" ones, built on
    kube-prometheus-stack's recording rules;
  - "Namespace (Objects)", built on the object watches the collector does not
    run;
  - OpenObserve's "Internals" and "Infrastructure", built for a cluster
    (querier, ingester pods). Its own `zo_*` metrics are scraped
    (`ZO_PROMETHEUS_ENABLED`, the chart's ServiceMonitor) into one of ours
    instead.
- **Written here**, for what the repository has nothing for, or nothing
  that works here: the Kubernetes API server, Traefik, Longhorn, Vault,
  CrowdSec, etcd and OpenObserve itself. PromQL over what the collector
  scrapes; every query was checked against OpenObserve before it was
  committed.

**Changing or adding one:** edit it in the UI in any folder but `0xc0`,
export it (dashboard → settings → export JSON), save it here as
`dashboards/<name>.json`, one-space indented, add it to `configMapGenerator`
in `kustomization.yaml`, and open a PR. The title is its key: renaming it is a
delete and a create.

The ConfigMap holds every dashboard, and a ConfigMap's limit is 1 MiB: about
700 KiB today, most of it the cluster overview. Past roughly 900 KiB, split
the files into a second ConfigMap mounted next to the first.

## Retention and size

Measured on 2026-10-12, ten days in (#68), with the last week's days, which
the compactor had finished:

| | Now | Per day | At 30 days |
|---|---|---|---|
| Bytes on the filesystem | 8.8 of 52.5 GB | ~0.8 GB | ~25 GB, 48% |
| Inodes | 0.81 of 3.28 million | ~73,000 | ~2.25 million, 69% |
| Longhorn, per replica, after its trim | 9.7 GB | | |

- Metrics are 98% of what comes in (about 40 GB a day before compression)
  and, with their index streams, 96% of what is stored: 347 MB a day, plus
  173 MB of index. Logs and traces store about 9 MB a day each.
- The inodes follow the number of streams, not their bytes. Each of the
  ~1,500 metrics and index streams gets a directory and a file per hour, so
  a new metric name costs about 48 inodes a day, however small it is.
- The filesystem holds 1.44 times the files' bytes, from those directories
  and the tails of small files.
- The compactor runs about two hours behind. Until then, each hour sits as
  ~37,000 small files, about 0.36 GB. Compacted, it is ~1,500 files and
  24 MB.

So a longer retention needs a larger volume, for the inodes before the bytes.
Inodes grow with the volume (ext4 keeps one per 16 KiB):

| Retention | Bytes | Inodes | Fits in |
|---|---|---|---|
| 30 days | ~25 GB | ~2.25 million | 50 Gi: 48% and 69% |
| 60 days | ~48 GB | ~4.4 million | 80 Gi: 56% and 84%. 50 Gi runs out of inodes around day 44 |
| 90 days | ~72 GB | ~6.6 million | 120 Gi: 56% and 84%, past what vm-rke2-01, -02 and -03 can schedule today |

A PVC cannot shrink. Longhorn allocates only what is written, so the unused
half costs scheduling room on each replica's node, not disk.

Growing it: `volumeClaimTemplates` cannot change on a StatefulSet, so the
size goes on the PVC first (`kubectl -n openobserve patch pvc
data-openobserve-0`), which Longhorn and ext4 grow online. Then
`persistence.size` in `values.yaml` follows. `kubectl -n openobserve delete
statefulset openobserve --cascade=orphan` lets the sync recreate the
StatefulSet without touching the pod.

To measure it again, from the kubelet of the node that runs `openobserve-0`:

```sh
kubectl get --raw "/api/v1/nodes/$(kubectl -n openobserve get pod openobserve-0 -o jsonpath='{.spec.nodeName}')/proxy/stats/summary" \
  | jq '.pods[] | select(.podRef.name == "openobserve-0") | .volume[] | select(.pvcRef.name == "data-openobserve-0")
        | {usedBytes, capacityBytes, inodesUsed, inodes}'
```

## Alerts

There are none (operator decision, 2026-10-02). If any are added, mail can go
to sergio@0xc0.cc, which Cloudflare Email Routing forwards.

## Checking every target is up

The target allocator lists them:

```sh
kubectl -n openobserve-collector port-forward deploy/openobserve-collector-gateway-targetallocator 8080:8080
curl -s localhost:8080/jobs | jq 'keys'
```

[vault-write]: https://github.com/0xc0-labs/vault/blob/main/README.md#writing-or-rotating-a-secret
