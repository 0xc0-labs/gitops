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
- **Matchmark**, written here as well, but SQL over the traces stream
  `default`, not metrics: the spans of the factory worker (tasks, attempts,
  waits, agent runs, model calls and tools), filtered by the `app` and
  `project` variables, on the span itself: `chat` and `execute_tool` spans
  carry their own application, project and agent since `matchmark` v0.41.3
  (2026-11-22 13:30 UTC, 0xc0-labs/matchmark#203), so those panels need no
  subquery, and older spans, which carry none, drop out of them. Since the
  same version `gen_ai_usage_input_tokens` is the whole input, the cached part
  included; the figures before that date held the uncached part alone. A model
  call is a `chat` span with tokens: one response of the model can arrive as
  several spans, and only the first carries the usage. It has no cost panel:
  the stream's `gen_ai_usage_cost` is derived by OpenObserve from its own
  price table, and the usage record in Postgres is the cost's home. A panel
  reads only fields the stream has: a query naming one it lacks is an error,
  not an empty result. A column alias that equals a stream field's name breaks
  the `GROUP BY`: the aliases here are never one. The Failures tables link a
  row to its trace with a `from` and a `to` (an hour before the row, a day
  after it, in microseconds, in two labelled columns): the trace page looks
  only inside that range, and the last fifteen minutes with none.

**Changing or adding one:** edit it in the UI in any folder but `0xc0`,
export it (dashboard → settings → export JSON), save it here as
`dashboards/<name>.json`, one-space indented, add it to `configMapGenerator`
in `kustomization.yaml`, and open a PR. The title is its key: renaming it is a
delete and a create.

The ConfigMap holds every dashboard, and a ConfigMap's limit is 1 MiB: about
733 KiB today, most of it the cluster overview. Past roughly 900 KiB, split
the files into a second ConfigMap mounted next to the first.

## Retention and size

Measured on 2026-10-12, ten days in (#68), with the last week's days, which
the compactor had finished:

| | Now | Per day | At 30 days |
|---|---|---|---|
| Bytes on the filesystem | 8.8 of 52.5 GB | ~0.8 GB | ~25 GB, 48% |
| Inodes | 0.81 of 3.28 million | ~73,000 | ~2.25 million, 69% (projected; the volume held ~3.28 million, see below) |
| Longhorn, per replica, after its trim | 9.7 GB | | |

- Metrics are 98% of what comes in (about 40 GB a day before compression)
  and, with their index streams, 96% of what is stored: 347 MB a day, plus
  173 MB of index. Logs and traces store about 9 MB a day each.
- The traces stream holds what someone reads: Traefik's, Argo CD's, the
  sites' and Matchmark's spans, and the Temporal server's spans only inside a
  trace the API or the worker started. Its polls and persistence reads, three
  quarters of the stream in the week to 2026-11-22 (about 2.4 million of 3.2
  million spans), are not recorded: the sampler is set at the source
  (`apps/matchmark-temporal`, Server tracing). The 9 MB a day above was
  measured before Temporal existed.
- The inodes follow the number of streams, not their bytes. Each of the
  ~1,500 metrics and index streams gets a directory and a file per hour, so
  a new metric name costs about 48 inodes a day, however small it is.
- The filesystem holds 1.44 times the files' bytes, from those directories
  and the tails of small files.
- The compactor runs about two hours behind. Until then, each hour sits as
  ~37,000 small files, about 0.36 GB. Compacted, it is ~1,500 files and
  24 MB.

On 2026-11-25 the volume ran out of inodes at 50 Gi: 3,276,800 used of
3,276,800, with bytes at 75% (36.5 of 48.9 GiB). OpenObserve logged `No space
left on device (os error 28)` some 2,000 times a minute, and ingestion, the
compactor and the memtable flush failed until the volume was grown. At 80 Gi
the inodes went to 63% (3.29 of 5.24 million; 36.7 of 78.4 GiB). The
projection above was 2.25 million inodes at 30 days; the volume held about
1.5 times that, with more streams than when it was measured (Temporal,
Matchmark, agentgateway, Plane).

So a longer retention needs a larger volume, for the inodes before the bytes.
Inodes grow with the volume (ext4 keeps one per 16 KiB). Measured at 30 days,
and scaled for the rest:

| Retention | Bytes | Inodes | Fits in |
|---|---|---|---|
| 30 days | ~39 GB (36.7 GiB) | ~3.3 million | 50 Gi: 75% and 100%, it ran out. 80 Gi: 47% and 63% |
| 60 days | ~78 GB | ~6.6 million | 120 Gi: 63% and 84%. 80 Gi runs out of inodes around day 48 |
| 90 days | ~117 GB | ~9.9 million | 160 Gi: 71% and 94%, past what vm-rke2-01, -02 and -03 can schedule today |

A PVC cannot shrink. Longhorn allocates only what is written, so the unused
half costs scheduling room on each replica's node, not disk.

Growing it: `volumeClaimTemplates` cannot change on a StatefulSet, so the
size goes on the PVC first (`kubectl -n openobserve patch pvc
data-openobserve-0`), which Longhorn and ext4 grow online. Then
`persistence.size` in `values.yaml` follows. After that change merges, and
before the orphan delete, make Argo CD read the merge commit: a hard refresh
of the Application, and a check that `status.sync.revision` is that commit.

```sh
kubectl -n argocd annotate application openobserve argocd.argoproj.io/refresh=hard --overwrite
kubectl -n argocd get application openobserve -o jsonpath='{.status.sync.revision}{"\n"}'
```

Only then `kubectl -n openobserve delete statefulset openobserve
--cascade=orphan`, which lets the sync recreate the StatefulSet with the new
`volumeClaimTemplates`, without touching the pod. Afterwards the pod's uid
(`kubectl -n openobserve get pod openobserve-0 -o jsonpath='{.metadata.uid}'`)
must be the one it had before. The orphan delete done right after a merge, on
2026-11-25, let self-heal recreate the StatefulSet from the old revision
(50Gi), which restarted the pod and then failed with "Forbidden: updates to
statefulset spec".

To measure it again, from the kubelet of the node that runs `openobserve-0`:

```sh
kubectl get --raw "/api/v1/nodes/$(kubectl -n openobserve get pod openobserve-0 -o jsonpath='{.spec.nodeName}')/proxy/stats/summary" \
  | jq '.pods[] | select(.podRef.name == "openobserve-0") | .volume[] | select(.pvcRef.name == "data-openobserve-0")
        | {usedBytes, capacityBytes, inodesUsed, inodes}'
```

## Memory of a search

OpenObserve was killed for memory (`OOMKilled`, limit 3Gi then) on 2026-11-22 at
13:59 UTC, after a burst of searches over the traces stream: a 30-day trace
lookup, then the 36 queries of the Matchmark dashboard over 7 days, with the
Traces page open (#572). The session that wrote this could not query the
cluster, so what grew is read from v1.0.1's source, not measured:

- `ZO_MEMORY_CACHE_DATAFUSION_MAX_SIZE` is not a shared cap. Every search
  builds its own DataFusion runtime (`create_runtime_env`) with a memory pool
  of that size, so searches add up: 256 MB each before this change.
- Nothing in this edition limits how many searches run at once. Work groups
  and the query queue are enterprise features, and `ZO_QUERY_THREAD_NUM` sets
  the threads that read files, not the searches admitted. A dashboard tab
  sends all its panels at once.
- The pool bounds what DataFusion tracks, not what a query reads and decodes
  around it, so a search can use more than its pool.
- Three caches took a default share of the limit that the caps above do not
  cover: the file statistics cache (5%, 153 MB; now 64), the inverted index
  footer cache (5%, 153 MB) and the bloom footer cache (1%, 32 MB).

The limit was 4Gi (`limits.memory`), the operator's decision of 2026-11-22
(#579), after the pool went to 128 MB (#576); it is 6Gi since #647, which
also set the request to what the pod holds (below). Measured then, with no search
running, the container sat at 2,438 to 2,492 MiB (`kubectl top`, four samples
in two minutes), against the 3Gi it had. The caps below bound what each part
may take; the measure is what the process holds at rest, caches and memtable
included, so the two are not added.

| | MB |
|---|---|
| Limit (6Gi) | 6,144 |
| At rest, measured | ~2,450 |
| **Left for searches** | **~3,700** |

The caps inside that rest figure: memtable (`ZO_MEM_TABLE_MAX_SIZE`) 512, file
cache (`ZO_MEMORY_CACHE_MAX_SIZE`) 256, file statistics cache 64, footer
caches at their defaults 185.

At 128 MB a pool, twenty-nine heavy searches fit in what is left of 6Gi (twelve
did of 4Gi; with 3Gi, about four). That is a bound on a heavy search, not on the count: a dozen
panels still ask for about what is left, and the pool of one that goes past
its 128 MB makes that query fail instead of the process. The request is 3Gi,
what the pod held on 2026-11-25 (3,379Mi, 478m of CPU), so the scheduler sees
it. Its node (`vm-rke2-04`) used 6.3 of 11.4 GiB when the limit was measured.
That node also runs the task sandboxes, whose memory limits already add up to
more than it has, so the limit is a ceiling the node may not always have to
give.

`ZO_MEMORY_CIRCUIT_BREAKER_ENABLED` (reject past a ratio of the limit) exists
and is documented in one line. It is not set: which requests it rejects, and
whether ingestion is among them, is not in the source read here.

Matchmark's dashboard opens on one day, not seven, so a tab asks for a seventh
of the data. The traces stream also became lighter from 2026-11-22 (#561); a
week of it is lighter from 2026-11-29.

To check after the sync, without repeating the burst: in the `OpenObserve`
dashboard, watch the container's working set and `zo_*` memory while one
person opens the Matchmark dashboard over one day, then over 7 days, a tab at
a time; the working set should stay under ~5,500 MB, and a panel that is too
large should show an error, not a restart. `kubectl -n openobserve get pod
openobserve-0 -o jsonpath='{.status.containerStatuses[0].lastState}'` shows
whether a kill happened.

## A frozen stream

When a log stream passes `ZO_SCHEMA_MAX_FIELDS_TO_ENABLE_UDS` fields (1000
here; the chart's own default is 150) and has no user-defined schema yet,
OpenObserve saves one with the stream's fields at that moment. From then on,
every field outside it is stored in the hidden `_all` column, not as a column
of its own (#333).

A frozen stream looks like this: a field the events send has no column, a
filter on it shows "No Data Found", and the value is in `_all`:

```sql
SELECT _all FROM "<stream>" ORDER BY _timestamp DESC LIMIT 1
```

The fix is the operator's, in the UI: Streams, the stream, Schema Settings,
then the user-defined schema: remove it, or add the missing fields. Events
stored before the fix keep the field in `_all`. A stream freezes again once it
passes 1000 fields.

## Alerts

OpenObserve has none (operator decision, 2026-10-02). Alerts are Grafana's
(`platform/kube-prometheus-stack`, README, Alert rules).

## Checking every target is up

The target allocator lists them:

```sh
kubectl -n openobserve-collector port-forward deploy/openobserve-collector-gateway-targetallocator 8080:8080
curl -s localhost:8080/jobs | jq 'keys'
```

[vault-write]: https://github.com/0xc0-labs/vault/blob/main/README.md#writing-or-rotating-a-secret
