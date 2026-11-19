# matchmark

Matchmark's own agentgateway, object store (RustFS) and API (both below), in its namespace `matchmark`, from
`prod/`: the controller, its CRDs (`agentgateway-crds`, `agentgateway`,
both `v1.7.0` from `oci://cr.agentgateway.dev/charts`) and one `Gateway`
named `gateway`, class `agentgateway`, with one HTTP listener on 8080 that
admits routes of its own namespace only. The proxy is one Deployment the
controller creates for that Gateway; its Service is `ClusterIP`. There is no
route to a provider's API for the product's agents: the only one goes to a
stand-in (The route to the provider). `/claude` reaches Anthropic's for the
subscription probe alone (The subscription probe). Agents call the gateway from inside the cluster, and it is not
reachable from outside. Only the API is, over WARP.

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

## The API

`api.yaml`: one `Deployment` `api` (one replica) of the private image
`ghcr.io/0xc0-labs/matchmark-api`, FastAPI on port 8000, a `Service` `api` on
80 and an `HTTPRoute` for `https://api.matchmark.int.0xc0.cc` on Traefik's
Gateway, `sectionName: internal-matchmark`, WARP only, with no `public`
annotation. It serves `GET /healthz`, `POST /v1/agent-tokens` (behind the
service token) and `GET /.well-known/jwks.json` (open).

- **Pin**: the `images:` entry and `APP_VERSION` of the `release` ConfigMap in
  `prod/kustomization.yaml`, which the application's release job moves with a
  PR on every merge to its `main`; Matchmark has no staging. The image is
  pulled with `ghcr-pull-vault`, from `apps/shared/ghcr-pull`.
- **Vault**: the pod logs in with its `api` ServiceAccount (Kubernetes auth,
  mount `kubernetes`, role `matchmark-api`) with a projected token of audience
  `vault`, at `MATCHMARK_VAULT_SA_TOKEN_FILE`. It signs with the Transit key
  `matchmark-agent-tokens`, which never leaves Vault, and publishes its public
  half. `platform/vault/network-policy.yaml` admits its pods.
- **Telemetry**: traces and metrics over OTLP/HTTP to the collector gateway on
  4318 (`OTEL_EXPORTER_OTLP_ENDPOINT`), logs as JSON on stdout.
- **Network**: the API's port is open to Traefik's pods and to this
  namespace's gateway (controller and proxy, for the JWKS); it reaches Vault's
  8200 and the collector's 4318.

```sh
kubectl -n matchmark get pod -l app.kubernetes.io/name=matchmark-api   # Ready
curl https://api.matchmark.int.0xc0.cc/healthz                         # 200, over WARP
curl https://api.matchmark.int.0xc0.cc/.well-known/jwks.json           # one Ed25519 key, kid matchmark-agent-tokens-v1
```

### Before the first sync: the secrets

Written in Vault by the operator (`vault`'s README, Writing or rotating a
secret); nothing here holds a value:

| Path (mount `apps`) | Keys | Read by |
|---|---|---|
| `matchmark/api` | `service_token` (32 characters or more) | `matchmark-api`, into `MATCHMARK_SERVICE_TOKEN` |
| `matchmark/storage` | `access_key`, `secret_key` | `rustfs-root` |
| `shared/ghcr-pull` | `username`, `password` | `ghcr-pull`, the image pull |
| `matchmark/claude-subscription` | `oauth_token` | `claude-subscription`, the subscription probe |

## The route to the provider

`gateway-route.yaml`, `stand-in-provider.yaml`, `network-policy-provider.yaml`
and `proof.yaml`. A model call made with an agent token crosses the gateway to
the stand-in provider; one without a valid token, or for another model than
its token's, is refused.

- **The stand-in** (`stand-in-provider.yaml`): the private image
  `ghcr.io/0xc0-labs/matchmark-stand-in-provider`, pinned with the API's, at
  the same tag (`pin / check` fails when `APP_VERSION` is not that tag). It
  answers `POST /v1/messages` in the shape of Anthropic's Messages API with a
  `usage` derived from the request alone, checks no credential and holds no
  data. It stands in for a real provider until there is one (no provider key
  exists anywhere yet): the cost the gateway reads is the catalog's price for
  the model asked for, over the stand-in's token counts.
- **The route**: `HTTPRoute` `model-calls` on `gateway` sends `POST
  /v1/chat/completions` and `POST /v1/messages`, and nothing else, to the
  `AgentgatewayBackend` `anthropic`, whose host and port are the stand-in's
  Service, in plain HTTP. The backend's `policies.ai.routes` says which format
  each path speaks, and without it every path answers in OpenAI's:
  `/v1/chat/completions` is `Completions` (OpenAI's chat completions shape,
  `usage.prompt_tokens` and `completion_tokens`; the gateway translates it to
  the stand-in's Messages), and `/v1/messages` is `Messages` (Anthropic's
  shape, `usage.input_tokens` and `output_tokens`). Matchmark's agents use
  `/v1/chat/completions`; `/v1/messages` is for clients that speak Anthropic's
  API. The backend's `path` is `/v1/messages`: with `host` overridden and no
  `path`, the proxy forwards the client's path unchanged, and the stand-in
  answers 404 to `/v1/chat/completions`. The backend has no credential, and
  the proxy adds none.
- **The policy** `agent-token`, on that route, so on both paths: JWT authentication in `Strict`
  mode (issuer `https://api.matchmark.int.0xc0.cc`, audience
  `matchmark-gateway`, the JWKS fetched from the Service `api`), and one
  authorization of action `Require` whose expressions must all hold:
  `jwt.tenant == "default"` and `llm.requestModel == jwt.model`. They are
  `Require`s and not `Allow`s because rules of different levels accumulate
  and a single `Allow` at another level would widen what an `Allow` here
  grants; a `Require` always has to hold. Authentication and authorization
  share one policy: one that reads `jwt` without `jwtAuthentication` beside it
  denies everything while reporting Accepted. The validated token is removed
  before the request goes upstream.
- **Who made the call**: the `telemetry` policy adds the token's claims to
  the access log and the traces as `matchmark.tenant`, `.app`, `.project`,
  `.agent`, `.work_item`, `.task` and `.attempt`, beside the usage and cost
  fields the gateway writes. An expression that fails (no `work_item` in the
  token) leaves its attribute out. `task` and `attempt` stay out of every
  metric's labels.
- **Network**: the proxy reaches the stand-in's 8000 and the stand-in the
  collector's 4318; the proof's pod reaches the API and the proxy's 8080,
  which no other pod does yet.

### The proof

`proof.yaml`: a `Job` `gateway-proof-<APP_VERSION>`, one per deployed version,
running `proof.py` (a ConfigMap) with the API's image. It fails
unless: (1) the API issues a token (tenant `default`, app `smoke`, project
`matchmark`, agent `proof`, task `phase-0`, attempt 1, model
`claude-sonnet-4-5`); (2) a call to `/v1/chat/completions` with that token and
model answers 200 with `usage.prompt_tokens` 3 and `completion_tokens` 16 (the
stand-in's rule for `Say only: ok`); (3) the same call to `/v1/messages`
answers 200 with `usage.input_tokens` 3 and `output_tokens` 16; (4, 5) the
same calls for another model answer 403; (6, 7) the same calls without a
token answer 401. It prints each step's result and never
the token. It is not a sync hook, so a failing proof never
holds a sync back: the Job's name takes the version from the `release`
ConfigMap, a release makes a new Job and Argo CD prunes the previous one. It
makes two attempts at most, within three minutes (the second covers the proxy
still loading the policy). A failed Job leaves the application `Degraded`, a
completed one `Healthy`: that, and the Job's log, is where to look. A change of
configuration with no new version does not run it again; to run it by hand,
delete the Job and let Argo CD recreate it.

```sh
kubectl -n matchmark get job                               # gateway-proof-<version>, Complete
kubectl -n matchmark logs job/gateway-proof-<version>     # seven lines, each "ok"
kubectl -n matchmark logs deploy/gateway | grep matchmark.tenant   # the access log, with the claims
```

When a step fails, the cause of a refusal is in the proxy's access log, field
`error`: the client's answer is generic.

## The subscription probe

`claude-subscription.yaml`, `subscription-probe.yaml` and
`network-policy-subscription.yaml`. Matchmark's agents may run on Claude Code
with the operator's subscription (operator decision, 2026-11-19); this answers,
before anything is built on it, whether Claude Code's token for runs without a
browser works through the gateway.

- **The route**: `HTTPRoute` `claude` sends every path under `/claude` to the
  `AgentgatewayBackend` `claude-subscription`, rewriting the prefix away, so
  `/claude/v1/messages` reaches Anthropic as `/v1/messages`. The backend is
  `api.anthropic.com` on 443, over TLS, with **no credential of its own**: the
  `Authorization` the caller sends goes upstream as it is. Its
  `policies.ai.routes` types `/v1/messages` as `Messages`,
  `/v1/messages/count_tokens` as `AnthropicTokenCount` and the rest as
  `Passthrough`.
- **The policy** `claude-agent-token`: the same chain as `agent-token`, but the
  agent token is read from the header `Matchmark-Agent-Token`, because
  `Authorization` carries the subscription's token. Same issuer, audience and
  JWKS; `Require`s of tenant `default` and of the model asked for being the
  token's.
- **The secret**: Vault `apps/matchmark/claude-subscription`, key
  `oauth_token` (what `claude setup-token` prints), read by the
  `VaultStaticSecret` `claude-subscription` into `claude-subscription-vault`.
  Only the probe's pod reads it, as `CLAUDE_CODE_OAUTH_TOKEN`.
- **The probe**: `Job` `subscription-probe`, a plain Job and not a sync hook,
  at most two attempts in five minutes. It installs Claude Code `2.1.311` with
  npm (`CLAUDE_CODE_VERSION`) on a Node image pinned by digest, asks the API
  for an agent token (tenant `default`, app `smoke`, project `matchmark`,
  agent `subscription-probe`, task `harness`, attempt 1, model
  `claude-haiku-5-5`), and runs `claude -p` once with the base URL on the
  gateway's `/claude`, the agent token in `ANTHROPIC_CUSTOM_HEADERS` and the
  small model set to the same model. It prints Claude Code's JSON answer, with
  the usage it reports, and never a token.
- **It spends the operator's plan each time it runs.** The Job is not
  recreated by a release: to run it again, delete it and let Argo CD recreate
  it.
- **Network**: this is the namespace's first way out to the internet. The
  proxy reaches TCP 443 anywhere outside the private ranges
  (`proxy-to-internet`), and so does the probe's pod, to fetch Claude Code. A
  NetworkPolicy cannot name a host: what decides where a call goes is the
  gateway's routes, and the only one that leaves the cluster is `claude`.

```sh
kubectl -n matchmark logs job/subscription-probe                       # Claude Code's answer and usage
kubectl -n matchmark logs deploy/gateway | grep subscription-probe     # the access log: paths, models, usage, cost, claims
kubectl -n matchmark delete job subscription-probe                     # run it again
```

## Checking

```sh
kubectl -n matchmark get pods                 # controller and proxy Running
kubectl get gatewayclass agentgateway         # Accepted
kubectl -n matchmark get gateway gateway      # Programmed
kubectl -n matchmark get svc                  # the proxy's is ClusterIP
```
