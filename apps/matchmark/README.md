# matchmark

Matchmark's own agentgateway, object store (RustFS) and API (both below), in its namespace `matchmark`, from
`prod/`: the controller, its CRDs (`agentgateway-crds`, `agentgateway`,
both `v1.7.0` from `oci://cr.agentgateway.dev/charts`) and one `Gateway`
named `gateway`, class `agentgateway`, with one HTTP listener on 8080 that
admits routes of its own namespace only. The proxy is one Deployment the
controller creates for that Gateway; its Service is `ClusterIP`. There is no
route to a provider's API for the product's agents: the only one goes to a
stand-in (The route to the provider). `/claude` reaches Anthropic's for the
agents that will run on the Claude Agent SDK (The subscription route), and
`/mcp/tracker` reaches the `tracker` tools' server (tracker-mcp). Agents call the gateway from inside the cluster, and it is not
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
(`AgentgatewayPolicy` `telemetry`); `platform/opentelemetry-collector` admits
the namespace in `otlp-from-senders`. The chart's `ServiceMonitor` (controller,
`:9092`) and `PodMonitor` (proxy, `:15020`) are what the collector's target
allocator scrapes, and what Prometheus (`monitoring`) scrapes too:
`metrics-from-collector` admits both to those two ports.
The namespace's own services (API, intake API, factory worker, tracker MCP)
expose no metrics port: they push over OTLP, so no other monitor is wanted.

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

It holds one bucket, `matchmark-artifacts`, and one user whose policy allows
that bucket alone (Artifacts). Only the factory worker's pods and the Job that
creates the bucket connect to it: `rustfs-ingress` opens 9000 to those and to
no other pod.

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
  pulled with `ghcr-pull-vault`, from `apps/shared/ghcr-pull`. An image the
  application's release names (its `extra-images`) must have its `images:`
  entry here first: otherwise the release's `pin` step fails with "No images
  entry named …" and no promotion PR opens.
- **Vault**: the pod logs in with its `api` ServiceAccount (Kubernetes auth,
  mount `kubernetes`, role `matchmark-api`) with a projected token of audience
  `vault`, at `MATCHMARK_VAULT_SA_TOKEN_FILE`. It signs with the Transit key
  `matchmark-agent-tokens`, which never leaves Vault, and publishes its public
  half. `platform/vault/network-policy.yaml` admits its pods.
- **Telemetry**: traces and metrics over OTLP/HTTP to the collector gateway on
  4318 (`OTEL_EXPORTER_OTLP_ENDPOINT`), logs as JSON on stdout.
- **Trace link**: `MATCHMARK_TRACE_URL_TEMPLATE`, Grafana's Explore of the
  `tempo` data source (`https://grafana.int.0xc0.cc`, WARP only: an operator's
  convenience, a client never reaches it), with one `{trace_id}` where the API
  puts a task's trace id. Its panes are percent-encoded, since the API accepts
  no `{` or `}` beside its placeholders, and it carries no
  `{start_time_unix_micro}` or `{end_time_unix_micro}`: Grafana takes
  milliseconds, so the range is fixed at the last 14 days, Tempo's retention.
  A task's read, `mm status --task`, prints the link; absent, it shows only the
  id. Check: run a task, then open the link `mm status --task <id>` prints over
  WARP: it shows that task's trace.
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
| `shared/ghcr-pull` | `username`, `password` | `ghcr-pull`, the image pull; `apps/matchmark-sandboxes` reads it too, for its own namespace |
| `matchmark/sandbox` | `token_key` (32 characters or more) | `matchmark-sandbox`, into `MATCHMARK_SANDBOX_TOKEN_KEY` of the factory worker (The factory worker) |
| `matchmark/artifacts` | `access_key_id`, `secret_access_key` | `matchmark-artifacts`: the artifact user's key pair, read by the worker and by the Job that creates the user (Artifacts) |
| `matchmark/claude-subscription` | `oauth_token` | `claude-subscription`, into `CLAUDE_CODE_OAUTH_TOKEN` of the factory worker (The factory worker) |
| `matchmark/plane` | `api_key` | `matchmark-plane`, mounted as a file by the API and the factory worker (The tracker) |

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

`proof.yaml`: a `Job` `gateway-proof-<APP_VERSION>-r2`, one per deployed version,
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
kubectl -n matchmark get job                               # gateway-proof-<version>-r2, Complete
kubectl -n matchmark logs job/gateway-proof-<version>-r2     # ten lines, each "ok"
kubectl -n matchmark logs deploy/gateway | grep matchmark.tenant   # the access log, with the claims
```

When a step fails, the cause of a refusal is in the proxy's access log, field
`error`: the client's answer is generic.

## The usage record

`migrate.yaml`, `network-policy-usage.yaml` and, in
`platform/opentelemetry-collector`, the `logs/matchmark_usage` pipeline of the
gateway. The cost of a model call lands in `usage_ledger` on the shared
PostgreSQL, with its tenant, application, project and agent, from the API (its
own README, Usage record).

**The path of a row.**

1. The proxy's `telemetry` policy exports each call's access log over OTLP gRPC
   to the gateway (4317). A model call's record carries
   `gen_ai.usage.*`, `agw.ai.usage.cost.total` and the `matchmark.*` claims; a
   refused call carries no `gen_ai.usage.*`.
2. The pipeline `logs/matchmark_usage` takes the records from the OTLP receiver,
   keeps those with `gen_ai.usage.input_tokens` or `.output_tokens`, batches
   them and posts them as OTLP/HTTP protobuf, gzipped, to
   `http://api.matchmark.svc.cluster.local/otlp` (`/v1/logs` is added).
3. The API keeps the records that are a model call with usage and writes one
   row each: a batch delivered twice adds none.

**Loki is not held back.** The gateway's `logs` pipeline, to Loki, takes the
same records from the OTLP receiver and has its own exporter, so every model
call's access log is also in Loki, whatever the API does. The API's exporter
has its own queue (1000 batches) and its retries stop after two minutes: with
the API down or slow, the queue fills, then the records of this pipeline alone
are dropped, and nothing else the gateway sends changes. In Grafana, Explore,
`Loki`: `{k8s_namespace_name="matchmark"} | gen_ai_usage_input_tokens != ""` (Loki
turns the record's attributes into structured metadata, dots to underscores);
the rows that count are the API's, in `usage_ledger`.

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
kubectl -n matchmark logs job/gateway-proof-<version>-r2      # step 10: the rows
```

## The subscription route

`claude-subscription.yaml` and `network-policy-subscription.yaml`. Matchmark's
agents may run on the Claude Agent SDK with the operator's subscription
(operator decision, 2026-11-19), calling Anthropic's native API through the
gateway; the factory worker's agents do (The factory worker). It was proven on 2026-11-19 with a one-off probe, since removed:
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
  `claude setup-token` prints). Only the factory worker mounts it (The factory
  worker).
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
`apps/matchmark-temporal/prod/`: the chart `temporal` 1.8.0 (server 1.32.2,
UI 2.56.4) from `https://go.temporal.io/helm-charts`, in the namespace
`matchmark`. Four
server Deployments (`temporal-frontend`, `-history`, `-matching`, `-worker`),
the UI (`temporal-web`), and two Jobs: the schema and the creation of the
Temporal namespaces (`factory`, 30 days, and `platform`, 1 day:
[`matchmark-temporal`](../matchmark-temporal/README.md#namespaces)). The stores are two
databases on the shared PostgreSQL, `matchmark-temporal` and
`matchmark-temporal-visibility`, with a role each (`platform/postgres`);
their passwords come from Vault (`matchmark/temporal-database`,
`matchmark/temporal-visibility-database`) and a change restarts the server
Deployments. The frontend is `temporal-frontend:7233` inside the namespace;
its clients are the UI, the namespace Job, the API and the factory worker.

**Two Applications in one namespace.** `matchmark-temporal`
(`bootstrap/prod/matchmark-temporal.yaml`, its folder
`apps/matchmark-temporal/prod/`) and `matchmark` share the namespace and
nothing else. `matchmark-temporal` is declared by hand and the `apps`
ApplicationSet excludes its folder: its template would name the Application
and the namespace `matchmark-temporal` and put it in the project `apps`. A
sync that fails holds every later sync of its Application back (gitops#438),
and the API releases on every merge while Temporal rarely changes: apart,
neither holds the other. The namespace,
`default-deny`, `vault-secrets` and the VaultAuth are `matchmark`'s.

**No sync hooks.** The chart's schema Job is a `pre-install,pre-upgrade`
hook by default, which Argo CD runs as PreSync before the Application's
Secrets exist. `schema.useHelmHooks` and `server.config.namespaces.useHelmHooks`
are false, so both Jobs are plain Jobs named after the chart's version
(`temporal-schema-1-8-0-1`): a chart bump makes new ones. Never a
sync-wave in `schema.jobAnnotations`: the namespace Job takes the same
annotations and needs the frontend up. `skipTests` drops the test Pod, never
`skipHooks`. The one hook is `temporal-namespaces`
(`apps/matchmark-temporal/prod/namespaces.yaml`), in PostSync: it runs
after the Application is healthy, so the frontend and the Secrets exist, and
the chart's namespace Job is off.

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
the UI, the namespace Job, the API and the worker reach the frontend) and WARP, the only way to
`https://temporal.matchmark.int.0xc0.cc`. The route is on the
`internal-matchmark` listener, with no `public` annotation.

**Telemetry.** The server pods' metrics (`:9090`) are scraped by Prometheus
through the chart's `ServiceMonitor`s (`server.metrics.serviceMonitor`), one per
service; `temporal-metrics-from-collector` admits it. Traces go over OTLP gRPC
to the collector's gateway (`OTEL_*` in `server.additionalEnv`), only the
spans of a trace a client started
([`matchmark-temporal`](../matchmark-temporal/README.md#server-tracing)); logs are
JSON on stdout at `info`.

**Resources** are a start, to be measured after the first week: requests
only on CPU (the cluster's scarce resource), a memory limit of twice the
request.

**An upgrade** goes one minor version at a time. The chart's version brings
the server's and the admin tools' together, and they must match. The new
schema Job runs beside the new pods, which fail their schema-version check
and restart until it is done. A schema only moves forward; a binary can be
rolled back. Changing the retention of a namespace is a line in
`apps/matchmark-temporal/prod/namespaces.yaml`: the next sync updates it.

## The factory worker

`factory-worker.yaml` and `network-policy-worker.yaml`. The worker of the
`factory` task queue of Temporal's namespace `factory`: one `Deployment`
`factory-worker` (one replica, `Recreate`) of the private image
`ghcr.io/0xc0-labs/matchmark-factory-worker`, pinned in `prod/kustomization.yaml`
beside the API's (The API, Pin). It takes the runs the API starts, asks the API
for the definitions of the version a run names, issues the agent's token
(`POST /v1/agent-tokens`) and reads the run's spend (`GET /v1/usage/totals`),
and runs the agent with Claude Code through the gateway's `/claude` route (The
subscription route): the agent's token in `Matchmark-Agent-Token`, the
subscription's credential in `Authorization`. It calls nothing else but
GitHub's API, as the App `matchmark-bot` (GitHub App, below).

- **Sandboxes**: the worker creates and deletes the sandboxes of
  `matchmark-sandboxes` and talks to their runtime. It calls the API server
  with its ServiceAccount's token, which the pod now mounts (it did not
  before: it called no Kubernetes API). The Role is in
  `apps/matchmark-sandboxes/prod/worker-role.yaml` and is as small as the
  adapter's calls: `create`, `get` and `delete` on `sandboxes.agents.x-k8s.io`
  and `get` on `pods`, with no `list`, no `watch` and no Secret, so a stolen
  token cannot enumerate the namespace or read the subscription's token there.
  Settings: `MATCHMARK_SANDBOX_SANDBOXD_IMAGE` comes from the ConfigMap
  `sandboxd-image`, generated by the component
  `apps/matchmark-sandboxes/sandboxd-image/` that the sandbox proof includes
  too, so the pin is written once (bump it there, and raise the number of
  `sandbox-proof-N` and `worker-proof-N`: a Job's template cannot change);
  `MATCHMARK_SANDBOX_TOKEN_KEY` is `matchmark/sandbox`'s `token_key`. The other
  `MATCHMARK_SANDBOX_*` settings keep the adapter's defaults.
- **Artifacts**: `MATCHMARK_ARTIFACTS_ENDPOINT` and `_BUCKET`, and the key pair
  `matchmark/artifacts` into `_ACCESS_KEY_ID` and `_SECRET_ACCESS_KEY`
  (Artifacts). Each secret restarts the Deployment when its value changes.
- **GitHub App**: the worker acts on GitHub as `matchmark-bot`, an App owned
  by `0xc0-labs` that the operator created (2026-11-20). Its secret is
  `matchmark/github-app` (`app_id`, `installation_id`, `private_key`, a PEM),
  read by the `VaultStaticSecret` `matchmark-github-app` into
  `matchmark-github-app-vault`. `MATCHMARK_GITHUB_APP_ID` and
  `MATCHMARK_GITHUB_INSTALLATION_ID` are variables; the key is a **file**,
  `private_key` mounted read-only at `/run/secrets/github-app/`, mode 0440
  with the pod's `fsGroup` (a Secret's files belong to root, so 0400 would
  leave the worker's user out), and `MATCHMARK_GITHUB_APP_KEY_FILE` is its
  path. No other pod mounts it, and the sandboxes' namespace never sees it
  (the worker-proof Job does, being the worker's twin). The installation
  covers `0xc0-labs/matchmark-playground` alone, `Only select repositories`
  (an operator step: GitHub has no API for it), and the proof fails if it
  covers anything else. The App's permissions are `metadata: read`,
  `contents: write`, `pull_requests: write` and `checks: write`, and it has no
  events. `MATCHMARK_GITHUB_PROXY_URL` is the sandboxes' proxy,
  `http://egress-proxy.matchmark-sandboxes.svc.cluster.local:3128`: the
  adapter tunnels every request through it with `CONNECT` and none goes
  direct. **The proxy's list now serves the worker as well as the sandboxes**
  (`api.github.com` and `github.com` are in it): whoever changes
  `apps/matchmark-sandboxes/prod/allowed-hosts.txt` changes where the worker,
  which holds the App's key, the service token, the artifact keys and the
  subscription's token, can reach. The variable is set or absent, never
  empty: an empty one is a settings error in the worker.
- **Harness**: the API starts runs with `MATCHMARK_HARNESS=claude_agent_sdk`
  (`api.yaml`: an operator decision of 2026-11-19, kept here in Git with the
  deployment) and reaches Temporal at `MATCHMARK_TEMPORAL_ADDRESS`.
- **Secrets**: `MATCHMARK_SERVICE_TOKEN` is `matchmark/api`'s `service_token`,
  the one the API reads, generated; `CLAUDE_CODE_OAUTH_TOKEN` is
  `matchmark/claude-subscription`'s `oauth_token`, the operator's own
  subscription, written by the operator (what `claude setup-token` prints).
  Each `VaultStaticSecret` restarts the Deployment when its value changes. The
  worker refuses to start with `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or
  `CLAUDE_CODE_USE_BEDROCK`, `_VERTEX` or `_FOUNDRY` in its environment: never
  add one.
- **Only the API starts runs.** The worker takes its input's word, so who
  reaches Temporal's frontend is what decides who can start one: the policies
  admit the API and the worker there (`temporal-frontend-from-clients`, in
  `apps/matchmark-temporal/prod/`, the other Application) and no other pod.
  Nothing reaches the worker.
- **Network**: the worker reaches the API's 8000, the proxy's 8080, the
  frontend's 7233, the collector's 4318, the three API servers' 6443, the
  sandboxes' 8080 and 9090, RustFS's 9000 and the sandboxes' egress proxy's
  3128 (`factory-worker-egress`; the sandboxes' own ingress policies admit it,
  `sandbox-ingress` and `proxy-ingress`). The API admits it (`worker-to-api`),
  as does the gateway's proxy (`worker-to-proxy`); the API reaches the
  frontend (`api-to-temporal`). The way out to Anthropic is the gateway's,
  `proxy-to-internet`, already open for the `/claude` route; the way out to
  GitHub is the sandboxes' proxy, 3128 (GitHub App). The worker has no route
  of its own to any public address, and a direct connection fails.
- **Probe and grace**: the liveness probe is the worker's own, `python -m
  matchmark_factory_worker probe`, which reads the liveness file under
  `/tmp`; there is no readiness probe, it takes no traffic. `minReadySeconds`
  is 30, so the Deployment counts the pod available only after 30 s without a
  restart and a worker that crashes at start never reads `1/1`; each rollout
  takes that long more (`Recreate`), well inside the 600 s progress deadline.
  `terminationGracePeriodSeconds` is 60: the worker waits 30 s for the runs in
  flight, then cancels them and tells Temporal.
- **Resources**: requests 100m and 512Mi, a memory limit of 2Gi, no CPU limit:
  each run starts a Claude Code process of up to about 1 GiB. A start, to be
  measured with the first runs. `/tmp` is an `emptyDir` of 1Gi (each run's
  working files), read-only root filesystem.
- **Order**: the worker registers Temporal's search attributes when it
  starts, so it runs before the first run is started.

```sh
kubectl -n matchmark get pod -l app.kubernetes.io/name=matchmark-factory-worker  # Running
kubectl -n matchmark logs deploy/factory-worker                                  # polls the task queue factory
mm start analyst --idea "…"                                                      # over WARP: ends completed with a Spec
```

## The runner

`runner-proof.yaml`, `runner-proof/proof.py`. The runner is the program that
works a task inside a sandbox; its image,
`ghcr.io/0xc0-labs/matchmark-runner` (private, pulled with `ghcr-pull-vault`),
holds one self-contained tree at `/opt/runner`: its own Python, a venv with the
runner and the Claude Agent SDK with its Claude Code binary, and `bwrap`.

- **Pin**: the `images:` entry of `prod/kustomization.yaml`, tag and digest,
  beside the other three, tagged `v0.31.0`: that release was built from the
  commit of the pinned digest, and the registry carries the tag for the runner
  from the first release that lists it (a pull goes by the digest). The release refuses an image the
  kustomization does not list: the entry is what lets it move this one once
  the application adds the runner to its images.
- **How the worker reads it**: `MATCHMARK_RUNNER_IMAGE` in `factory-worker.yaml`
  is a placeholder that a `replacements` entry overwrites with the image the
  proof's init container renders, `<name>:<tag>@sha256:<digest>`. `images:`
  rewrites container images only, so that is the one place the pin reaches the
  setting: moving the entry moves both, and they cannot differ. The worker
  refuses a value without `@sha256:`. Rendered today:
  `ghcr.io/0xc0-labs/matchmark-runner:v0.31.0@sha256:ccdb75d4fcf7c38a824faddea145eb04a2407faf523723cb9787bfd207ee63b5`.
- **Subscription**: `MATCHMARK_RUNNER_SUBSCRIPTION` is `true`: the sandbox's
  workspace runs on the Claude subscription whose token is the worker's
  `CLAUDE_CODE_OAUTH_TOKEN`, which the worker hands to the runner when it
  starts it. `MATCHMARK_SANDBOX_IMAGE_PULL_SECRET` is `ghcr-pull-vault`: the
  pull Secret of the sandboxes' namespace, which the worker names in the pods
  it creates (`apps/matchmark-sandboxes`, README.md, The image pull Secret).
- **Proof**: `runner-proof-<APP_VERSION>-r3`, a plain Job in wave 2, one per
  deployed version like `gateway-proof` (a release moves the pin, a Job is
  immutable, so each release is a new Job and the previous one is pruned), not
  run again by a sync. It runs in `matchmark`, which can pull the image,
  in a pod with the sandbox's shape: `runtimeClassName: gvisor`, user 10001, no
  privilege escalation, every capability dropped, the default seccomp profile,
  a read-only root, no ServiceAccount token and no egress of its own. An init
  container copies the tree with `cp -a /opt/runner /work/` into a volume, as
  one directory the user owns (the volume's root is not its own, and `cp -a`
  of `.` fails on its times); the toolchain image (the playground's, `uv` on
  Python 3.12) mounts that directory (`subPath: runner`) read-only at
  `/opt/runner`, with `/opt/runner/bin` first in `PATH`. It prints one line per
  check, `ok <name>` or `FAILED <name>: <how>`, and ends non-zero when any
  failed:

| Check | Shows |
|---|---|
| `tree runs with its own python` | the venv's Python imports `matchmark_runner` and `claude_agent_sdk`, and nothing on its `sys.path` is outside the tree |
| `claude binary starts` | `_bundled/claude`, the file the Agent SDK starts, runs `--version` there; the line says which file ran, and a bundle without it fails with what the bundle holds |
| `socat runs from the tree` | `/opt/runner/bin/socat -V` exits 0 as the runner's user: Claude Code's sandbox on Linux needs it beside `bwrap` |
| `bwrap --version from the tree` | the tree's own `bwrap` and libraries load |
| `bwrap makes user, pid and mount namespaces` | a command under `bwrap` ends well in namespaces of its own with a `/proc` of its own, and its `/proc/1/environ` is not the pod's first process's |
| `a non-dumpable process cannot be read` | a process that sets `PR_SET_DUMPABLE` to 0 cannot have its `/proc/<pid>/environ` or `/proc/<pid>/mem` read by another of the same user; the same reads of a process that did not set it must succeed, or the check fails as blind |

The last two are what Matchmark's design leaves to the cluster. If one fails,
the Job fails, and its line says how.

- **Sandbox proof**: `runner-sandbox-proof-<APP_VERSION>-r2`
  (`runner-sandbox-proof.yaml`, `runner-proof/sandbox-proof.py`), a plain Job
  in wave 2, one per deployed version like the one above, with the worker's
  ServiceAccount, labels and image (as `worker-proof-1`), so it has the
  worker's rights and network. It makes a `Sandbox` in `matchmark-sandboxes`
  the way the worker's adapter does: the runner copied by an init container
  from the private image (the kustomization's one pin, copied into the Job's
  `MATCHMARK_RUNNER_IMAGE` by the same `replacements` entry as the worker's) as
  one directory mounted by `subPath`, the pull Secret named, `sandboxd` with its
  token. It shares the quota's one `Sandbox` with the other proofs and waits for
  the slot. One line per step, `ok` or `FAILED`:

| Step | Shows |
|---|---|
| `sandbox ready (private image pulled, runner copied)` | the namespace's pull Secret pulled the private image and the copy worked |
| `pull Secret named by the pod` | the pod carries the Secret's name |
| `runner imports from the tree` | through `sandboxd`, the tree's Python imports `matchmark_runner` |
| `bwrap runs from the tree` | through `sandboxd`, `/opt/runner/bin/bwrap --version` |
| `bwrap makes its namespaces in a sandbox` | through `sandboxd`, `bwrap` makes its user, pid and mount namespaces (the check `runner-proof` makes in a plain pod) |
| `sandbox deleted and none left` | the Sandbox is deleted and reads as gone |

```sh
kubectl -n matchmark logs job/runner-proof-<version>-r3    # each line "ok", then "all checks ok"
kubectl -n matchmark logs job/runner-sandbox-proof-<version>-r2    # each step "ok", then "all steps ok"
kubectl -n matchmark get pod -l app.kubernetes.io/name=matchmark-factory-worker -o yaml | grep -A1 MATCHMARK_RUNNER_IMAGE
```

## Artifacts

`artifacts.yaml`, `artifacts/`, `network-policy-artifacts.yaml`. The factory
worker keeps its artifacts (patches, logs, transcripts) in RustFS, with a
credential of its own and not the root one.

- **Bucket**: `matchmark-artifacts`, with a lifecycle rule that aborts an
  incomplete multipart upload after a day.
- **Credential**: a RustFS user whose key pair is `matchmark/artifacts`, and the
  policy `matchmark-artifacts` (`artifacts/policy.json`), which allows that
  bucket alone: list it and its multipart uploads, and get, put and delete its
  objects, multipart included. The sandboxes never get these keys.
- **Setup**: `artifacts-setup-2`, a plain Job in wave 1, the only user of the
  root credentials. Its init container runs `rc` (RustFS's CLI,
  `rustfs/rc:v0.1.38`, pinned by digest) through `artifacts/rc-setup.sh`: the
  alias, the policy, the user and the policy's attachment; its container,
  `artifacts/setup.py`, creates the bucket, sets the lifecycle rule and reads
  it back. Every step passes when its result already exists (the policy is
  written again, the user added only when missing, the attachment accepted
  when the user carries it), so a run after a half-finished one needs nothing
  removed by hand. It is not run again by a sync. To run it again (a rotated
  key pair, a changed policy), raise the number in its name.
- **Proof**: `worker-proof-5`, a plain Job in wave 2, with the worker's
  ServiceAccount and labels, so it has the worker's rights and network, and
  the secrets the worker reads (the App's key mounted as a file the same way).
  It prints one line per step and ends `all steps ok`. It prints nothing of
  the key, the JWT or a token:

| Step | Shows |
|---|---|
| `resolves egress-proxy`, `resolves gateway` | the worker resolves the two names the adapter uses |
| `sandbox created`, `sandbox ready`, `pod read` | with the worker's Role, a `Sandbox` is created in `matchmark-sandboxes`, becomes ready, and its pod's address is read |
| `runtime reached with the derived token` | `/v1/metadata` on 8080 answers with `hex(HMAC-SHA256(key, "sandboxd-token:" + name))`, the token the adapter derives |
| `runtime refuses a call without the token` | the second wall |
| `list of sandboxes refused`, `get of a Secret there refused` | the Role gives no more than the adapter calls |
| `sandbox deleted` | the Sandbox is deleted and gone |
| `object put`, `object read back` | the artifact credential writes and reads |
| `multipart upload started`, `part uploaded`, `multipart upload completed`, `multipart object read back`, `multipart upload aborted` | the calls the adapter's multipart put makes, and an abort |
| `bucket listed`, `objects deleted`, `no object left behind` | the credential lists the bucket and deletes (`DeleteObjects`) what it wrote |
| `second bucket not created`, `second bucket not listed` | the policy ends at the bucket |
| `key is a file only its user and group read`, `key is in no variable` | the App's key is a mounted file, read-only for the worker's user, and in no variable |
| `proxy accepts a connection` | the proxy takes a connection, waiting up to five minutes for the rule that admits the worker (`matchmark-sandboxes` can sync after this Job starts) |
| `api.github.com answers through the proxy` | the `CONNECT` tunnel to GitHub's API opens and GitHub answers |
| `host outside the list refused` | the proxy answers `403` to a host that is not in its list |
| `direct connection to a public address fails`, `proxy not reached on another port` | the worker's only way out is the proxy's 3128 |
| `pip installs through the proxy`, `JWT signed with the mounted key` | `cryptography` comes through the proxy, and the mounted key signs the App's JWT |
| `the App answers to its JWT`, `the App's permissions are the design's four`, `the App has no events` | the App is `metadata: read`, `contents: write`, `pull_requests: write`, `checks: write` and nothing else |
| `the installation answers`, `installation covers selected repositories`, `installation's permissions are the four` | the installation is `selected`, with the same four |
| `token made for the installation`, `installation's only repository is 0xc0-labs/matchmark-playground`, `token revoked` | a token is made from the key and the installation lists that repository alone: the proof fails if it covers anything else. The token is revoked at the end |

The proof is plain Python (`artifacts/s3.py` signs SigV4 requests), and
installs `cryptography`, pinned, through the sandboxes' proxy to sign the
App's JWT: the worker's network has no other way out. The lifecycle rule is read back by the setup Job with the root
credentials, as the artifact user may not read a bucket's configuration. The
proof waits for the one `Sandbox` the quota allows, so it can run beside
`sandbox-proof-N`, which waits for it as well.

```sh
kubectl -n matchmark logs job/artifacts-setup-2 -c bucket   # lifecycle rule read back
kubectl -n matchmark logs job/worker-proof-5                # all steps ok
```

## The tracker

`network-policy-tracker.yaml`, `matchmark-plane` in `vault-secrets.yaml`, and
the settings of `api.yaml` and `factory-worker.yaml`. The API and the factory
worker reach Plane, the platform's tracker, as its service user, a member of
the project `PLAY` (Playground) of the workspace `0xc0`. A task written there
and moved to its ready state is found by the worker's poll and started, and
its item follows the run; no webhook is needed for that.

- **Settings**, on both: `MATCHMARK_TRACKER_ADDRESS` is
  `http://plane-api.plane.svc.cluster.local:8000`, Plane's API inside the
  cluster; `MATCHMARK_TRACKER_WORKSPACE` is `0xc0`;
  `MATCHMARK_TRACKER_API_KEY_FILE` is `/run/secrets/plane/api_key`. The three
  are set together or not at all (a half is an error in the code). The API
  also has `MATCHMARK_TASK_APP=factory`, the application that runs tasks. No
  webhook secret: only `intake-api` will hold one. The worker alone also has
  `MATCHMARK_TRACKER_WEB_ADDRESS`, `https://plane.int.0xc0.cc`: the address
  people open the tracker at, which the factory's pull requests link their
  task by (`.../0xc0/browse/<key>/`). It is optional, with no secret and no
  path, and separate from `MATCHMARK_TRACKER_ADDRESS`, which the API is
  called at.
- **Key**: `matchmark/plane`'s `api_key`, written by the operator, read by the
  `VaultStaticSecret` `matchmark-plane` into `matchmark-plane-vault`. It is a
  **file**, mounted read-only at `/run/secrets/plane/`, mode 0440 with the
  pod's `fsGroup` (the API's has one for this), never a variable. Only the API,
  the worker and the worker's proof Job mount it. To rotate it: create the new
  key in Plane as the service user, write it with `vault`'s README, Writing or
  rotating a secret, and revoke the old one; the secret refreshes within an
  hour and restarts both Deployments.
- **Network**: `tracker-egress` opens the API's and the worker's egress to
  Plane's `plane-plane-api` pods on 8000, and `api-from-matchmark`, in
  `apps/plane/prod/network-policy.yaml` (the other Application), admits those
  two to them. Plane reaches nothing new.
- **Proof**: `worker-proof-5` (Artifacts) ends with the steps `tracker key is
  a file only its user and group read`, `tracker key is in no variable`,
  `Plane's API accepts a connection` (it waits up to five minutes, as for the
  proxy), `Plane answers the key` (`GET /api/v1/users/me/` is 200), `project
  PLAY is in workspace 0xc0` and `project PLAY has every state the mapping
  uses` (`Backlog`, `Todo`, `In Progress`, `In Review`, `Blocked`, `Done`,
  `Cancelled`). It prints no key.

```sh
kubectl -n matchmark logs job/worker-proof-5 | grep -i -e plane -e tracker  # all ok
kubectl -n matchmark logs deploy/factory-worker | grep -i tracker          # its poll, not "not configured"
```

## tracker-mcp

`tracker-mcp.yaml`, `network-policy-tracker-mcp.yaml`, `tracker-mcp-route.yaml`,
`tracker-mcp-proof.yaml` and `tracker-mcp-proof/proof.py`. The MCP server that
gives an agent the `tracker` tools (`0xc0-labs/matchmark`,
`services/tracker-mcp/README.md`; the design is its
`docs/architecture/tracker-tools.md`): `get_item`, `get_comments`,
`get_related`, `comment`, `ask_person` and `create_subitem`. It holds no key,
no secret and no Vault role, and mounts no ServiceAccount token: it passes the
agent's token on to the API, which judges every call.

- **Pin**: `ghcr.io/0xc0-labs/matchmark-tracker-mcp` in the `images:` of
  `prod/kustomization.yaml`, one tag with the others. The application's release
  lists it in its `extra-images`, so its release job moves this entry in the
  promotion PR with the rest: that is what keeps the promotions running, and
  without the entry the job's `pin` step fails (The API, Pin).
- **The server**: a `Deployment` and a `Service` `tracker-mcp`, one replica, on
  port 8000 (the Service's 80). Its settings are three and none is a secret:
  `MATCHMARK_TRACKER_MCP_API_ADDRESS` (the API's Service), `..._API_TIMEOUT_SECONDS`
  (15, above the API's own 10 seconds of waiting for the tracker's limiter) and
  `..._PORT`. Probes on `/healthz`; the same pod security context as
  `intake-api`, a read-only root and `/tmp`; 25m and 96Mi requested, 192Mi
  limit, for one process with no state. It keeps no data: no volume, no
  backup. Logs, metrics and traces go to the collector, service name
  `matchmark-tracker-mcp`.
- **The route**: `HTTPRoute` `tracker-mcp` sends the path prefix `/mcp/tracker`
  of the gateway's listener to the `AgentgatewayBackend` `tracker-mcp`, whose
  one target, `tracker`, is the Service, over streamable HTTP, with `path:
  /mcp`, which is what the server serves. There is no rewrite: the gateway
  speaks MCP on the route's path and opens its own connection to the target's
  path. The backend is `Stateless` (the server keeps no session) and
  `prefixMode: Never`, so a tool keeps its name (`mcp__tracker__get_item` in a
  client). The route is not on the WARP route: only agents in the cluster reach
  it.
- **The policy** `tracker-mcp-token`: the same chain as `agent-token`
  (issuer, audience `matchmark-gateway`, the JWKS from the Service `api`,
  `jwt.tenant == "default"` as a `Require`, expiry by the authentication), with
  the agent's token in `Authorization`; and the grants, as one `Allow` in
  `backend.mcp.authorization` over `mcp.tool.name` and the token's `grants`
  claim: `tracker-read` opens `get_item`, `get_comments` and `get_related`;
  `tracker-write` opens `comment`, `ask_person` and `create_subitem`, and
  only for a token with an `item`. A tool no rule allows is left out of
  `tools/list` and refused on `tools/call`.
- **What the gateway does with a grant**, as seen on 2026-11-23
  (`0xc0-labs/workspace#282`): a run's token with no grant is listed no tool
  and its `tools/call` is refused by the gateway; a token with `tracker-read`
  and `tracker-write` is listed its tools and reaches the API (`get_item`,
  `get_comments` and `ask_person` answered 200). So the gateway reads the
  claim. The proof can only issue a token with no grants, since a grant comes
  from an agent's definition: to such a token, a gateway that denies every
  tool looks the same as one that reads the claim, and only an agent's run
  shows the difference. If the rule denies what it should allow, the fix is to
  delete the `backend` block: the API is the authority either way.
- **Network**: `tracker-mcp-ingress` admits the gateway's proxy on 8000 and
  nothing else; `tracker-mcp-egress` opens the API's 8000 and the collector's
  4318; `proxy-to-tracker-mcp` opens the proxy's way to the server's 8000;
  `api-ingress` also admits `tracker-mcp`. DNS is the namespace's `dns`. The
  sandboxes need no new rule: they already reach the proxy's 8080
  (`sandbox-to-proxy`), and the route is on that listener.
- **The proof**: a `Job` `tracker-mcp-proof-<APP_VERSION>`, one per deployed
  version, in sync wave 1, running `proof.py` with the API's image and the
  gateway proof's label, so it takes that proof's network. It fails unless:
  (1) the API issues a token; (2, 3) `tools/list` with no token, and with a
  token that is not ours, answer 401; (4) `initialize` with the token answers
  the server's name; (5) `tools/list` with the token answers a list; (6) that
  list is empty for a token with no grant; (7) a `tools/call` of `get_item`
  with such a token is refused by the gateway: not by the server for want of a
  token, not by the API as a tool error. It runs in the matchmark namespace,
  not from a sandbox: the token is issued with the service token, which only
  this namespace has. A read that succeeds needs a token with
  `tracker-read` and an item the tracker knows, so it waits for the first agent
  that has them.

```sh
kubectl -n matchmark get pod -l app.kubernetes.io/name=matchmark-tracker-mcp   # Ready
kubectl -n matchmark get agentgatewaybackend tracker-mcp                       # Accepted
kubectl -n matchmark get agentgatewaypolicy tracker-mcp-token                  # Accepted, Attached
kubectl -n matchmark logs job/tracker-mcp-proof-<version>                      # steps 1-7
kubectl -n matchmark logs deploy/gateway | grep mcp/tracker                    # the access log, with the claims
```

If the first pull is refused (`ImagePullBackOff`, denied), the cluster's pull
credential has no access yet to the private package `matchmark-tracker-mcp`:
the operator grants it in the package's settings on GitHub.

## intake-api

`intake-api.yaml`, `network-policy-intake.yaml`, `matchmark-intake-api` in
`vault-secrets.yaml`, and the settings of `api.yaml`. The door for what a
connector sends signed, starting with Plane's webhook (`0xc0-labs/matchmark`,
`services/intake-api/README.md`). It verifies the signature and hands the event
to the API's `POST /v1/tracker/events`; it keeps nothing, and holds one secret
of the connector (its webhook's) and one token (its own): never Plane's API key,
never the token-signing key, no Temporal address. It has no route: only Plane's
worker reaches it, inside the cluster, at
`http://intake-api.matchmark.svc.cluster.local` (port 80, the pod's 8000).

- **Pin**: `ghcr.io/0xc0-labs/matchmark-intake-api` in the `images:` of
  `prod/kustomization.yaml`, with the API's tag and the digest of the image
  built from that release's commit. The application's release lists it in its
  `extra-images`, so its release job moves the pin with the others.
- **Settings**: `MATCHMARK_INTAKE_API_ADDRESS` is the API's Service,
  `MATCHMARK_INTAKE_API_TOKEN_FILE` the token's file, and
  `MATCHMARK_INTAKE_API_CONNECTORS` is empty: it starts, says in its log that
  it serves no connector, and answers every webhook `404`. The Plane
  connector's settings are in `intake-api.yaml` as a commented block, with their
  values, ready to turn on; a connector with some of its settings and not all
  stops the start, which is why it stays off until its secret exists.
- **Secrets**, both read by a `VaultStaticSecret` and mounted as a read-only
  file, mode 0440, never a variable:

  | Vault path (mount `apps`) | Key | Who mounts it | State |
  |---|---|---|---|
  | `matchmark/intake-api` | `token` | `intake-api`, and the API as `MATCHMARK_API_INTAKE_TOKEN_FILE`, so it accepts the door on `POST /v1/tracker/events` alone | written |
  | `matchmark/plane-webhook` | `secret` | `intake-api` | not yet: Plane generates it with the webhook (below) |

  The `VaultStaticSecret` of the second is commented in `vault-secrets.yaml`
  until the secret exists. To rotate the token: write a new one with `vault`'s
  README, Writing or rotating a secret; both Deployments restart.
- **Network**: `intake-api-ingress` admits Plane's `plane-plane-worker` pods on
  8000 and nothing else; `intake-api-egress` opens the API's 8000 and the
  collector's 4318, and DNS is the namespace's `dns`; `api-ingress` also
  admits `intake-api`. On Plane's side, `webhooks-to-matchmark`
  (`apps/plane/prod/network-policy.yaml`) opens the worker's egress to
  `intake-api`'s pods on 8000.
- **Plane's allow list**: Plane delivers a webhook to a private address only
  when told to. A patch in `apps/plane/prod/kustomization.yaml` sets
  `WEBHOOK_ALLOWED_HOSTS=intake-api.matchmark.svc.cluster.local` on its `api`
  and `worker`, naming that host alone; the chart has no value for it.

**To turn the webhook on** (the operator's steps, once this is synced):

1. In Plane, as an admin of the workspace `0xc0`, create a webhook in its
   settings to
   `http://intake-api.matchmark.svc.cluster.local/v1/webhooks/plane`, for
   work items and their comments.
2. Write the secret Plane shows to Vault at `apps/matchmark/plane-webhook`, key
   `secret` (`vault`'s README, Writing or rotating a secret).
3. A small change here uncomments the connector's settings, its volume and the
   `VaultStaticSecret`.

```sh
kubectl -n matchmark get pod -l app.kubernetes.io/name=matchmark-intake-api  # Ready
kubectl -n matchmark logs deploy/intake-api | grep -i connector               # serves no connector
```

## Checking

```sh
kubectl -n matchmark get jobs -l app.kubernetes.io/name=temporal   # both Complete
kubectl -n matchmark get deploy -l app.kubernetes.io/name=temporal # five available
curl https://temporal.matchmark.int.0xc0.cc/api/v1/namespaces      # over WARP: lists factory and platform
kubectl -n matchmark get pods                 # controller and proxy Running
kubectl get gatewayclass agentgateway         # Accepted
kubectl -n matchmark get gateway gateway      # Programmed
kubectl -n matchmark get svc                  # the proxy's is ClusterIP
curl -i -X POST https://api.matchmark.int.0xc0.cc/v1/messages         # over WARP: 401 from the gateway
curl -i -X POST https://api.matchmark.int.0xc0.cc/claude/v1/messages  # over WARP: 401, no Matchmark-Agent-Token
curl -i https://api.matchmark.int.0xc0.cc/healthz                     # over WARP: the API's 200
```
