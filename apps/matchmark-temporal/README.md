# matchmark-temporal

Matchmark's Temporal, in `prod/`. Why it is a separate Application and how it
is declared: [`apps/matchmark/README.md`](../matchmark/README.md#temporal).

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
