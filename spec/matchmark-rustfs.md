# Matchmark's object store: RustFS in its namespace

Issue: 0xc0-labs/gitops#429. Branch: `feat/matchmark-rustfs`.

## Spec

Matchmark has one RustFS server in the `matchmark` namespace, from
`apps/matchmark/prod`. Its artifacts (patches, logs, transcripts, screenshots,
design prototypes) go there later: operator decision, 2026-11-19, an exception
to the org's rule against an object store in the cluster, as Plane's
attachments are.

- One `StatefulSet` `rustfs`, one replica, the official image
  `rustfs/rustfs` pinned by digest: 1.0.6 (stable, published 2026-11-15).
- One `ClusterIP` `Service` `rustfs`, the S3 API on 9000 alone. The console is
  switched off (`RUSTFS_CONSOLE_ENABLE=false`) and no Service names its port.
- Root credentials from Vault, `apps/matchmark/storage`, keys `access_key` and
  `secret_key`, through a `VaultStaticSecret` and the namespace's `VaultAuth`
  (role `apps`), as `apps/plane` writes them.
- Data on one 10Gi Longhorn volume, `Prune=false`, with its backup declared.
- Pod Security `restricted`; requests (CPU 100m at most) and a memory limit;
  probes on `/health` (liveness) and `/health/ready` (readiness).
- NetworkPolicy: nothing connects to it yet; it initiates only OTLP to the
  collector.
- Logs: to stdout, which the node collector already ships. Metrics: pushed by
  OTLP over HTTP to the collector gateway on 4318 (the namespace is already
  admitted in `otlp-from-senders`).
- `apps/matchmark/README.md` documents it.

Out of scope: a bucket, a user or a policy; any route from outside the
namespace; the RustFS on the node.

## Plan

1. `rustfs.yaml`: Service, StatefulSet with a `volumeClaimTemplates` `data`
   (the StatefulSet's own claim is not pruned by Argo CD, and the
   annotation goes on the template).
2. `vault-secrets.yaml`: ServiceAccount, VaultAuth, VaultStaticSecret.
3. `network-policy.yaml`: egress to the collector's OTLP HTTP port.
4. `kustomization.yaml`: list the new resources, pin the image in `images:`.
5. README section.
6. Render with `kustomize build --enable-helm` and validate with `kubeconform`.

## Tasks

- [x] rustfs.yaml
- [x] vault-secrets.yaml
- [x] network-policy.yaml
- [x] kustomization.yaml
- [x] README
- [x] render and validate
