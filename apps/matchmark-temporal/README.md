# matchmark-temporal

Matchmark's Temporal, in `prod/`. Why it is a separate Application and how it
is declared: [`apps/matchmark/README.md`](../matchmark/README.md#temporal).

## Namespaces

| Namespace | What runs in it | Retention |
|---|---|---|
| `factory` | The work of the application: the runs a person looks at | 30 days |
| `platform` | Plumbing with no agent and no person behind it, the tracker's poll first | 1 day |

Retention counts from the moment a workflow closes, so a run stays that long
after it ends. 1 day is Temporal's minimum. The tracker's poll closes about
480 runs a day, which is why it has a namespace of its own: in `factory` it
filled the list a person reads (0xc0-labs/workspace#269).

`prod/namespaces.yaml` holds both in `NAMESPACES` and is the only place that
does: a sync hook (PostSync) that creates a namespace that does not exist and
sets the retention of one that does, so a change in git reaches the cluster
with the next sync and no command by hand. It authenticates to nothing: the
frontend has no authorizer, and the Job carries the labels the network
policies already let reach `temporal-frontend:7233`. The chart's own namespace
Job is off, as it never updates one. A namespace is never deleted from here:
taking one out of the list leaves it in Temporal.

To see what Temporal has, over WARP:

```sh
curl https://temporal.matchmark.int.0xc0.cc/api/v1/namespaces/factory   # config.workflowExecutionRetentionTtl
curl https://temporal.matchmark.int.0xc0.cc/api/v1/namespaces/platform
```

## Server tracing

The server keeps only the spans that belong to a trace a client started: a
workflow's start, a signal or an update sent by the API or the worker, whose
trace context the SDK's interceptor puts in the request. The polls and the
persistence reads (`PollActivityTaskQueue`, `ListNamespaces`,
`GetHistoryTasks`, ...) are roots of their own, run whether or not a
workflow does, and were three quarters of the traces stream (0xc0-labs/gitops#560).

`OTEL_TRACES_SAMPLER=parentbased_always_off` in `prod/values.yaml` does it at
the source: a span with a sampled remote parent is recorded, a root is not,
so a task's trace still opens whole. It names no service or span, so the
collector filters nothing. To see the server's own spans again while
debugging, set `parentbased_always_on` and sync; put it back after.
