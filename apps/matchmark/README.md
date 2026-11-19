# matchmark

Matchmark's own agentgateway and object store (RustFS, below), in its namespace `matchmark`, from
`prod/`: the controller, its CRDs (`agentgateway-crds`, `agentgateway`,
both `v1.7.0` from `oci://cr.agentgateway.dev/charts`) and one `Gateway`
named `gateway`, class `agentgateway`, with one HTTP listener on 8080 that
admits routes of its own namespace only. The proxy is one Deployment the
controller creates for that Gateway; its Service is `ClusterIP`. There is no
route, no `AgentgatewayBackend` and no provider yet: agents will call it from
inside the cluster, and nothing here is reachable from outside. Matchmark's
API, its agent tokens and the provider's route come in their own changes.

## Why this application has a project of its own

agentgateway is part of Matchmark and is Matchmark's alone (operator decision,
2026-11-19), so it is deployed with it, not as a component of `platform/`. It
brings cluster-scoped kinds, which the `apps` project does not admit. The
`matchmark` `AppProject` (`bootstrap/prod/projects.yaml`) admits exactly:
`Namespace`, `CustomResourceDefinition`, `ClusterRole` and
`ClusterRoleBinding`, and only the destination namespace `matchmark`. The
`apps` ApplicationSet gives that project to this Application alone.

The cost: a change under `apps/matchmark/prod` can create cluster-wide
permissions. Review every PR there with that in mind; `pin / check` keeps a
bot's PR to image pins.

## What the controller can reach

The chart's `ClusterRole` `agentgateway-matchmark` reads beyond the
namespace: `get`, `list` and `watch` of namespaces, nodes, pods, services,
**configmaps and secrets** in every namespace, endpointslices, Gateway API
and `agentgateway.dev` kinds, CRDs, Deployments, DaemonSets, ServiceAccounts,
HPAs and PodDisruptionBudgets; `create` of `tokenreviews`; and `create`,
`update` and `patch` of `gatewayclasses` (the controller owns the
`agentgateway` class). The chart cannot narrow those reads. Its writes it can:
`rbac.gatewayNamespaces: [matchmark]` binds the `-deployer` role (create,
delete, patch and update of ConfigMaps, Secrets, ServiceAccounts, Services,
Deployments, DaemonSets, HPAs and PodDisruptionBudgets) per namespace, so the
controller writes only in `matchmark`. `discoveryNamespaceSelectors` narrows
what it builds configuration from to `matchmark`.

## Telemetry

The proxy sends its access log and traces over OTLP gRPC to the collector
(`AgentgatewayPolicy` `telemetry`); `platform/openobserve-collector` admits
the namespace in `otlp-from-senders`. The chart's `ServiceMonitor` (controller,
`:9092`) and `PodMonitor` (proxy, `:15020`) are what the collector's target
allocator scrapes.

## Pod Security

The namespace enforces `restricted`. The chart sets no pod `seccompProfile`:
`values.yaml` sets it for the controller, and `AgentgatewayParameters` for the
proxy's pod. The proxy's container security context is the controller's
default, which this repo does not set.

## RustFS

Matchmark's object store, for its artifacts (patches, logs, transcripts,
screenshots and the design prototypes), "for this application, for now"
(operator decision, 2026-11-19: not Cloudflare R2; an exception to the org's
rule against an object store in the cluster, as Plane's attachments are).
`rustfs.yaml`: one `StatefulSet` `rustfs` with one replica, `rustfs/rustfs`
`1.0.6` (a stable release) pinned by digest, and a `ClusterIP` `Service`
`rustfs` for the S3 API on 9000. The web console is off
(`RUSTFS_CONSOLE_ENABLE=false`) and no Service exposes 9001. One server and
one volume: Longhorn keeps three replicas of the volume.

There is no bucket, no user and no policy, and nothing connects to it: the
NetworkPolicies admit no ingress to 9000. The first consumer brings its bucket,
a credential narrower than the root one, and the policy that opens 9000 to it.

- **Secret**: Vault `apps/matchmark/storage`, keys `access_key` and
  `secret_key`, the root credentials, read by a `VaultStaticSecret` into the
  Secret `rustfs-root-vault` (`vault-secrets.yaml`). Writing it:
  `vault`'s README, Writing or rotating a secret.
- **Backup**: PBS's nightly backup of the RKE2 VMs, which holds the Longhorn
  volume `data-rustfs-0` (10Gi). Its claim is not pruned.
- **Telemetry**: logs go to stdout, read by the node collector like every
  pod's; metrics are pushed over OTLP/HTTP to the collector gateway on 4318,
  which `rustfs-otlp-to-collector` and the collector's `otlp-from-senders`
  allow.

```sh
kubectl -n matchmark get pod rustfs-0                      # Ready
kubectl -n matchmark get pvc data-rustfs-0                 # Bound, 10Gi
kubectl -n matchmark exec rustfs-0 -- ls /data             # the volume is mounted
kubectl -n matchmark get svc rustfs                        # ClusterIP, 9000 only
```

To check the backup, look for the VM's last PBS job in the Proxmox backup
datastore, and restore the volume's data from it.

## Checking

```sh
kubectl -n matchmark get pods                 # controller and proxy Running
kubectl get gatewayclass agentgateway         # Accepted
kubectl -n matchmark get gateway gateway      # Programmed
kubectl -n matchmark get svc                  # the proxy's is ClusterIP
```
