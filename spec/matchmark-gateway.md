# Matchmark's gateway

Issue: 0xc0-labs/gitops#427. Branch: `feat/matchmark-gateway`.

## Spec

Matchmark gets its namespace and, in it, its own agentgateway 1.7.0: the
controller, its CRDs and one `Gateway` with its proxy, deployed by Argo CD from
`apps/matchmark/prod`. The Gateway has no route and no provider. Its logs,
metrics and traces reach OpenObserve. Nothing is reachable from outside the
cluster.

Acceptance criteria: the issue.

## Plan

1. `apps/matchmark/prod/`: a Kustomization with the two OCI charts at
   `v1.7.0`, the Namespace, the NetworkPolicies, an `AgentgatewayParameters`
   (ClusterIP Service, proxy image by digest, resources, OTLP env) and the
   `Gateway`, plus an `AgentgatewayPolicy` for the access log and traces.
2. `bootstrap/prod/projects.yaml`: an `AppProject` `matchmark` whitelisting
   the cluster-scoped kinds of the render; `bootstrap/prod/apps.yaml` gives
   the `matchmark` Application that project.
3. `platform/openobserve-collector/network-policy.yaml`: `matchmark` in
   `otlp-from-senders`.
4. `apps/matchmark/README.md`.
5. Verify: `kustomize build --enable-helm` and `kubeconform` on the render,
   and on every component this touches.

## Tasks

- [x] Render and list the cluster-scoped kinds and the `ClusterRole` rules.
- [x] Project, ApplicationSet, collector policy.
- [x] Application folder, README.
- [x] Local render and validation; PR; checks.

## Decisions to verify against the charts

- Metrics are scraped through the chart's `ServiceMonitor` and `PodMonitor`
  (`monitoring.enabled`), as the collector's target allocator reads them.
- The proxy's image is pinned by `AgentgatewayParameters.spec.image.digest`;
  the controller's by Kustomize `images:`.
