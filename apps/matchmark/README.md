# matchmark

Matchmark's own agentgateway, object store (RustFS) and API (both below), in its namespace `matchmark`, from
`prod/`: the controller, its CRDs (`agentgateway-crds`, `agentgateway`,
both `v1.7.0` from `oci://cr.agentgateway.dev/charts`) and one `Gateway`
named `gateway`, class `agentgateway`, with one HTTP listener on 8080 that
admits routes of its own namespace only. The proxy is one Deployment the
controller creates for that Gateway; its Service is `ClusterIP`. There is no
route to a provider's API for the product's agents: the only one goes to a
stand-in (The route to the provider). `/claude` reaches Anthropic's for the
agents that will run on the Claude Agent SDK (The subscription route). Agents call the gateway from inside the cluster, and it is not
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
annotation. It serves `GET /healthz`, `POST /v1/agent-tokens` and `GET
/v1/usage` (behind the service token), `GET /.well-known/jwks.json` (open) and
`POST /otlp/v1/logs`, which only the collector reaches (The usage record). The
route names `/healthz`, `/v1/` and `/.well-known/`, so a request for `/otlp`
over WARP answers 404.

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
- **Network**: the API's port is open to Traefik's pods, to this
  namespace's gateway (controller and proxy, for the JWKS) and to the
  collector's gateway pods (The usage record); it reaches Vault's 8200, the
  collector's 4318 and PostgreSQL's 5432.

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
| `matchmark/database` | `password` | `matchmark-database`, into `PGPASSWORD` of the API and the migration. The role `matchmark` on the shared PostgreSQL: `platform/postgres` sets it |
| `matchmark/storage` | `access_key`, `secret_key` | `rustfs-root` |
| `shared/ghcr-pull` | `username`, `password` | `ghcr-pull`, the image pull |
| `matchmark/claude-subscription` | `oauth_token` | nothing: no pod mounts it (The subscription route) |

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
token answer 401; (8) a call to `/v1/chat/completions` that forces a tool
(Pydantic AI's request for a typed output: `tools` with `strict`,
`tool_choice: "required"`, `max_completion_tokens`) answers 200 with
`choices[0].message.tool_calls[0]` named `final_result` and `arguments` equal to
`{"title": "", "kind": "feature", "done": false}`, the input the stand-in
derives from the tool's schema; (9) the same call to `/v1/messages` (`tools`
with `input_schema`, `tool_choice: {"type": "any"}`) answers 200 with
`stop_reason` `tool_use` and `content[0]` a `tool_use` block named
`final_result` with that `input`. Steps 8 and 9 prove that a typed output
crosses the gateway on both ways in, the translation of the request and of the
`tool_use` block included; a failing one prints the status and the first 300
characters of the body, which tell the gateway's refusal from the stand-in's;
(10) the API's `GET /v1/usage` for tenant `default`, agent
`proof`, since the proof started, shows the two plain calls (steps 2 and 3) as
rows with 3 input and 16 output tokens, a cost above zero, app `smoke` and
project `matchmark`, waiting up to a minute for them (The usage record). It prints each
step's result and never
the token. It is not a sync hook, so a failing proof never
holds a sync back: the Job's name takes the version from the `release`
ConfigMap, a release makes a new Job and Argo CD prunes the previous one. It
runs in sync wave 1, after the API's new pods are Healthy. It
makes two attempts at most, within seven minutes (the second covers the proxy
still loading the policy; each attempt may wait the minute of step 10). A failed Job leaves the application `Degraded`, a
completed one `Healthy`: that, and the Job's log, is where to look. A change of
configuration with no new version does not run it again; to run it by hand,
delete the Job and let Argo CD recreate it.

```sh
kubectl -n matchmark get job                               # gateway-proof-<version>, Complete
kubectl -n matchmark logs job/gateway-proof-<version>     # ten lines, each "ok"
kubectl -n matchmark logs deploy/gateway | grep matchmark.tenant   # the access log, with the claims
```

When a step fails, the cause of a refusal is in the proxy's access log, field
`error`: the client's answer is generic.

## The usage record

`migrate.yaml`, `network-policy-usage.yaml` and, in
`platform/openobserve-collector`, the patch that adds the `logs/matchmark_usage`
pipeline. The cost of a model call lands in `usage_ledger` on the shared
PostgreSQL, with its tenant, application, project and agent, from the API (its
own README, Usage record).

**The path of a row.**

1. The proxy's `telemetry` policy exports each call's access log over OTLP gRPC
   to the collector's gateway (4317). A model call's record carries
   `gen_ai.usage.*`, `agw.ai.usage.cost.total` and the `matchmark.*` claims; a
   refused call carries no `gen_ai.usage.*`.
2. The pipeline `logs/matchmark_usage` takes the records from the OTLP receiver,
   keeps those with `gen_ai.usage.input_tokens` or `.output_tokens`, batches
   them and posts them as OTLP/HTTP protobuf, gzipped, to
   `http://api.matchmark.svc.cluster.local/otlp` (`/v1/logs` is added).
3. The API keeps the records that are a model call with usage and writes one
   row each: a batch delivered twice adds none.

**OpenObserve is not held back.** No other pipeline takes logs from the OTLP
receiver, so the API's pipeline is the only one on that signal; metrics and
traces are other signals, with their own pipelines. The exporter has its own
queue (1000 batches) and its retries stop after two minutes: with the API down
or slow, the queue fills, then the records of this pipeline alone are dropped,
and nothing else the collector sends changes. The OpenObserve exporters are
untouched.

**The database.** `PGHOST=postgres.postgres.svc.cluster.local`,
`PGDATABASE` and `PGUSER` `matchmark`, `PGPASSWORD` from Vault
`apps/matchmark/database` through the `VaultStaticSecret` `matchmark-database`,
which restarts the `api` Deployment when it changes. `PGSSLMODE=disable`: the
shared PostgreSQL serves no TLS, as `artistlabco-com` sets for it
(`DATABASE_SSL=false`); the traffic stays in the cluster, under policies that
admit the API's pods and the migration's alone, at both ends
(`network-policy-usage.yaml` and `platform/postgres/network-policy.yaml`).

**The migration.** `Job` `api-migrate`, an Argo CD `Sync` hook in wave -1,
deleted before each creation, so every sync runs it: `python -m matchmark_api
migrate` with the API's image and the same `PG*` variables. It is idempotent.
The secrets and the policy it needs are in wave -2 and the API's new pods in
wave 0, so a version's pods start after its migration succeeded; a migration
that fails (two attempts at most, within three minutes) fails the sync there
and the previous pods keep serving.

**Only the collector reaches `/otlp`.** The endpoint asks for no credential, and
the tenant of a row is what the record says. The public route names the paths
it serves, so over WARP `/otlp/v1/logs` answers 404, and
`api-otlp-from-collector` opens the API's port to the collector's gateway pods
alone outside this namespace, besides Traefik's.

```sh
kubectl -n matchmark logs job/api-migrate                  # the migrations applied
curl -i https://api.matchmark.int.0xc0.cc/otlp/v1/logs     # 404, over WARP
kubectl -n matchmark logs job/gateway-proof-<version>      # step 10: the rows
```

## The subscription route

`claude-subscription.yaml` and `network-policy-subscription.yaml`. Matchmark's
agents may run on the Claude Agent SDK with the operator's subscription
(operator decision, 2026-11-19), calling Anthropic's native API through the
gateway. The route is deployed ahead of them: **nothing calls it until those
agents run.** It was proven on 2026-11-19 with a one-off probe, since removed:
Claude Code, on the subscription's token, works through the gateway (findings
in 0xc0-labs/gitops#440).

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
- **The secret**: the subscription's token stays in Vault,
  `apps/matchmark/claude-subscription`, key `oauth_token` (what
  `claude setup-token` prints). **Nothing syncs or mounts it**: the change that
  deploys the first agent adds the `VaultStaticSecret` again.
- **Network**: this is the namespace's first way out to the internet. The
  proxy reaches TCP 443 anywhere outside the private ranges
  (`proxy-to-internet`). A NetworkPolicy cannot name a host: what decides where
  a call goes is the gateway's routes, and the only one that leaves the cluster
  is `claude`.
- **Over WARP**: the route is also reached from outside the cluster, under
  `/claude/` of `api.matchmark.int.0xc0.cc` (The gateway over WARP), so a
  person's machine can run Claude Code through the gateway with its own
  credential in `Authorization`.

## The gateway over WARP

`gateway-warp.yaml`. The gateway answers under the API's name
(operator decision, 2026-11-19: `api.matchmark.int.0xc0.cc` is the API, the
gateway included), so an agent can run from a person's machine (`mm run`)
against the same gateway and rules as the cluster's own agents.

- **The route**: `HTTPRoute` `gateway-warp`, on the same `internal-matchmark`
  listener and host as `api`, sends `/v1/chat/completions` and `/v1/messages`
  (exact) and everything under `/claude/` to the Service `gateway`, port 8080.
  Every other path stays with the API. Gateway API's precedence decides it,
  across routes too: an exact match beats a prefix, so the API's `/v1/` prefix
  never takes those two. Token counting reaches the gateway under
  `/claude/v1/messages/count_tokens`, which the prefix covers. Internal only,
  with no `public` annotation.
- **Who may call** does not change: `agent-token` still asks for the agent's
  token in `Authorization` on the model routes, and `claude-agent-token` in
  `Matchmark-Agent-Token` under `/claude/`. A person's own Claude credential,
  in `Authorization` under `/claude/`, goes to Anthropic untouched, as the
  worker's will; the gateway holds no provider credential on that route.
- **Streaming**: a streamed answer (server-sent events) is not buffered by
  Traefik: it flushes `text/event-stream` as it arrives, the route carries no
  buffering or compression Middleware, and the entrypoints set no
  `writeTimeout`, so nothing cuts a long answer.
- **Network**: `gateway-ingress` admits Traefik's pods to the proxy's 8080,
  selecting it by `gateway.networking.k8s.io/gateway-name: gateway`, as
  `api-ingress` does for the API.

```sh
curl -i -X POST https://api.matchmark.int.0xc0.cc/v1/messages                # 401, from the gateway
curl -i -X POST https://api.matchmark.int.0xc0.cc/claude/v1/messages         # 401: no Matchmark-Agent-Token
curl -i https://api.matchmark.int.0xc0.cc/healthz                            # 200, the API's
```

## Temporal

Temporal, the engine Matchmark's agents run in as workflows, from
`temporal/`: the chart `temporal` 1.8.0 (server 1.32.2, UI 2.56.4) from
`https://go.temporal.io/helm-charts`, in the namespace `matchmark`. Four
server Deployments (`temporal-frontend`, `-history`, `-matching`, `-worker`),
the UI (`temporal-web`), and two Jobs: the schema and the creation of the
Temporal namespace `factory` (retention 7 days). The stores are two
databases on the shared PostgreSQL, `matchmark-temporal` and
`matchmark-temporal-visibility`, with a role each (`platform/postgres`);
their passwords come from Vault (`matchmark/temporal-database`,
`matchmark/temporal-visibility-database`) and a change restarts the server
Deployments. The frontend is `temporal-frontend:7233` inside the namespace;
no client of it is admitted yet.

**Two Applications in one namespace.** `matchmark-temporal`
(`bootstrap/prod/matchmark-temporal.yaml`) and `matchmark` share the
namespace and nothing else. A sync that fails holds every later sync of its
Application back (gitops#438), and the API releases on every merge while
Temporal rarely changes: apart, neither holds the other. The namespace,
`default-deny`, `vault-secrets` and the VaultAuth are `matchmark`'s.

**No sync hooks.** The chart's schema Job is a `pre-install,pre-upgrade`
hook by default, which Argo CD runs as PreSync before the Application's
Secrets exist. `schema.useHelmHooks` and `server.config.namespaces.useHelmHooks`
are false, so both Jobs are plain Jobs named after the chart's version
(`temporal-schema-1-8-0-1`): a chart bump makes new ones. Never a
sync-wave in `schema.jobAnnotations`: the namespace Job takes the same
annotations and needs the frontend up. `skipTests` drops the test Pod, never
`skipHooks`.

**Pools.** The chart leaves a pool without an upper bound, and Temporal's
sample (20, four services, two stores) reaches 160 connections; the shared
server allows 100 to every application together. `maxConns` and
`maxIdleConns` are 8 on the default store and 4 on visibility, 48 at most,
with `maxConnLifetime: 1h` on both. They go away with a PostgreSQL of
Matchmark's own.

**Shards.** `numHistoryShards: 128`, fixed at the first start for the life
of the databases: it can **never** be changed. Every shard polls the database
a few times a minute even at rest, so a small server takes 128 where a
production cluster takes 512.

**No authentication.** Without an authorizer the frontend allows every
request, and the UI is as open as the frontend. `TEMPORAL_ALLOW_NO_AUTH`
says so. What stands in its place: the namespace's network policies (only
the UI and the namespace Job reach the frontend) and WARP, the only way to
`https://temporal.matchmark.int.0xc0.cc`. The route is on the
`internal-matchmark` listener, with no `public` annotation.

**Telemetry.** The server pods' metrics (`:9090`) carry `prometheus.io/*`
annotations, as the collector's scrape reads them; traces go over OTLP gRPC
to the collector's gateway (`OTEL_*` in `server.additionalEnv`); logs are
JSON on stdout at `info`.

**Resources** are a start, to be measured after the first week: requests
only on CPU (the cluster's scarce resource), a memory limit of twice the
request.

**An upgrade** goes one minor version at a time. The chart's version brings
the server's and the admin tools' together, and they must match. The new
schema Job runs beside the new pods, which fail their schema-version check
and restart until it is done. A schema only moves forward; a binary can be
rolled back. Changing the retention of `factory` later is
`temporal operator namespace update`: the Job never updates one.

## Checking

```sh
kubectl -n matchmark get jobs -l app.kubernetes.io/name=temporal   # both Complete
kubectl -n matchmark get deploy -l app.kubernetes.io/name=temporal # five available
curl https://temporal.matchmark.int.0xc0.cc/api/v1/namespaces      # over WARP: lists factory
kubectl -n matchmark get pods                 # controller and proxy Running
kubectl get gatewayclass agentgateway         # Accepted
kubectl -n matchmark get gateway gateway      # Programmed
kubectl -n matchmark get svc                  # the proxy's is ClusterIP
curl -i -X POST https://api.matchmark.int.0xc0.cc/v1/messages         # over WARP: 401 from the gateway
curl -i -X POST https://api.matchmark.int.0xc0.cc/claude/v1/messages  # over WARP: 401, no Matchmark-Agent-Token
curl -i https://api.matchmark.int.0xc0.cc/healthz                     # over WARP: the API's 200
```
