# bootstrap/prod

What the root `Application` syncs: the `AppProject`s, and the ApplicationSets
that turn `platform/*` and `apps/*` into `Application`s (`CLAUDE.md` says
which directory becomes what).

## How an Application syncs

Every `Application` syncs automatically from `main`, with `prune` and
`selfHeal`, and retries a failed sync: up to 10 attempts, 30 s apart,
doubling up to 5 minutes. The retry is what orders components that need
another's CRDs.

A failed sync is not abandoned: it is retried until the limit, and each
attempt waits for the Deployments' progress deadline. A retry keeps the
revision its sync started with, so without `retry.refresh` a commit that
takes the application back would wait behind the retries of the broken one:
more than an hour in the worst case (#597).

Every retry sets `refresh: true` ([Argo CD's automated sync][auto-sync],
"Automatic Retry Refresh on new revisions"): each attempt takes the latest
revision of `main`. A rollback merged while a sync is retrying is applied by
the next attempt, within the backoff (5 minutes at most), with no hand on
Argo CD.

On the cluster, `spec.syncPolicy.retry.refresh` of the live `Application` is
`true`:

```sh
kubectl -n argocd get application <name> -o jsonpath='{.spec.syncPolicy.retry.refresh}'
```

### Terminating a running operation by hand

For a rollback that cannot wait for the next attempt, or once the retries are
spent: terminate the operation, and the automated sync starts again from the
latest revision.

```sh
argocd app terminate-op <name>
```

Without the CLI, `kubectl -n argocd patch application <name> --type merge
-p '{"operation": null}'` clears the pending operation.

[auto-sync]: https://argo-cd.readthedocs.io/en/release-3.5/user-guide/auto_sync/#automatic-retry-refresh-on-new-revisions
