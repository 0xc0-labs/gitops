# gitops

What runs in the Kubernetes cluster, as manifests ArgoCD applies.

- `bootstrap/prod/` — what the root `Application` syncs; the `argocd` role in
  `infrastructure` writes that root. It holds the `AppProject`s and the
  `platform` ApplicationSet, which turns every directory under `platform/`
  into an `Application` of its name, in the `platform` project.
- `platform/<component>/` — one Kustomize component per shared service: a
  `kustomization.yaml` whose `helmCharts` inflate 1..n charts, each pinned by
  version with its `values*.yaml`; `resources` for what the charts do not bring
  (its `Namespace`, with its Pod Security and Gateway labels, and its
  NetworkPolicies); `patches` for tweaks. Adding a component is adding its
  directory. Its Application's destination namespace is the directory's name:
  Argo CD puts there what a chart leaves without one, because Kustomize's
  `namespace` field does not reach what `helmCharts` inflates. A component
  holding data or CRDs sets `Prune=false` through `commonAnnotations`.
- `apps/<application>/` — the applications, the same pattern under the
  `apps` ApplicationSet and its narrower `AppProject`: namespaced resources
  only, and their own Namespace. An application is one folder with one
  directory per environment, each its own `Application`: `prod/` is
  `<application>`, in the namespace of that name; `staging/`, when it has
  one, is `<application>-staging`, in its own. What both share goes in
  `base/`. Today: Mautic, offby1-cc (the offby1.cc landing page) and Payload
  CMS, each in prod alone.
- ArgoCD itself is installed and upgraded by RKE2's helm-controller, never
  from here.

Operator decision, 2026-09-29: an ApplicationSet over Kustomize
components, instead of hand-written `Application`s with inline values.

ArgoCD syncs `bootstrap/prod/`, and through it `platform/` and `apps/`, from
`main` (workspace `docs/design.md`).

## What goes where

One RKE2 cluster, in the `platform` zone, holds every shared service and
application:

- **Shared services**: ArgoCD, Vault, OpenObserve (metrics, logs, traces), and
  the data services: one shared MariaDB.
- **Applications**.

They are separated by namespace and NetworkPolicy. Traffic enters through the
HAProxy load balancer in front of the cluster (layer 4), and the ingress,
Traefik, carries the WAF: CrowdSec's bouncer.

The network is decided by `../infrastructure/environments/prod/terraform.tfvars`
(`zones`, `vms`, `transit`) and explained in `../infrastructure/docs/zones.md`.

## Hard rules

- Everything written is in English: files, file names, comments, commits,
  branches and PRs.
- No work without an issue on the org project board. The PR links it
  (`Closes #N` / `Refs owner/repo#N`) or the `issue` check fails. See the
  workspace `CLAUDE.md`, section Tracking.
- **Images pinned by digest.** Never `latest`, never a tag alone.
- **TLS ends at Traefik**, with a Let's Encrypt wildcard per domain from
  cert-manager (`platform/traefik/certificates.yaml`), on the HTTPS listeners
  of the Gateway. Routes attach to those, never to `web`, which only
  redirects. Adding a domain: its Certificate, its two listeners (`*.d` and
  `d`), and its zone in the Cloudflare token.
- **A name is public only by annotation**: external-dns publishes the
  HTTPRoutes marked `gateway.0xc0.cc/public: "true"`, and only in the domains
  of its `domainFilters` (the zones in the public tunnel's Cloudflare account,
  also listed in infrastructure's `public_domains`). A portal never carries
  it.
- **Two paths in**, separated by network. Public and WARP traffic reaches the
  public VIP (`10.10.4.10`) and `websecure`. The WARP-only path reaches the
  internal VIP (`10.10.4.9`) and Traefik's `internal` entrypoint, which the
  public tunnel never targets. An internal service attaches to the Gateway's
  `internal` listener (`sectionName: internal`) with a `*.int.0xc0.cc` name,
  its namespace labelled `gateway.0xc0.cc/internal: "true"`, and never
  carries the `public` annotation. external-dns never publishes under
  `int.0xc0.cc`. A `*.int.0xc0.cc` name never goes on a `websecure` route
  either: the public `*.0xc0.cc` listener would accept it. The internal path
  has no WAF: CrowdSec's bouncer is on `websecure` only.
- **The WAF lives at the ingress**: CrowdSec's bouncer on Traefik's
  entrypoint. Do not duplicate it in a service. Behind Cloudflare, the client
  is `CF-Connecting-IP`, trusted only from `platform`.
- **Security headers in two layers**. Traefik sets the baseline on
  every entrypoint (`platform/traefik/security-headers.yaml`: HSTS with
  subdomains, nosniff, Referrer-Policy); an application never repeats it.
  What depends on the application (framing, Permissions-Policy, COOP/CORP)
  goes in a headers Middleware on its own route, and the CSP comes from the
  application itself.
- **Every namespace denies by default** and opens only what it needs, with
  NetworkPolicies. Data services accept connections and initiate none.
- **Every persistent volume declares its backup** in a comment: destination
  and frequency. Without that, the service is not deployed.
- **Secrets:** all from Vault, never in this repo, not even encrypted
  (.github#6). What RKE2 and ArgoCD boot with (the RKE2 token, ArgoCD's admin
  password) Ansible reads from Vault in `infrastructure`; everything else
  reaches the cluster through Vault Secrets Operator. A component gets its
  secrets through Vault Secrets Operator: its own
  `VaultAuth` (Kubernetes auth, role named after its namespace, defined in the
  `vault` repo) and a `VaultStaticSecret` or `VaultDynamicSecret` per secret.
  It reads only `platform/<namespace>/*` (an application, `apps/<namespace>/*`),
  and the `shared/<name>` secrets its policy grants by name.
  Paths are kebab-case, keys inside snake_case.
- **Portals** (OpenObserve, ArgoCD) are internal: reached only over WARP, never
  published (operator decision, 2026-09-29). **Vault, the Kubernetes API and
  other admin interfaces are never published** either.

## Promotion

Each application decides whether it has staging. One that does promotes
staging→prod with **the same digest**: the image is never rebuilt
between environments. One that does not deploys the digest from `main`
straight to prod.

## Before opening a PR

For changes touching exposure or NetworkPolicies, check against the hard
rules above:

- Only a route meant to be public carries `gateway.0xc0.cc/public: "true"`,
  in a domain of external-dns's `domainFilters`. A portal, Vault, the
  Kubernetes API or another admin interface never does.
- A public route attaches to an HTTPS listener of `websecure`, never `web`.
  An internal one attaches to `sectionName: internal` with a
  `*.int.0xc0.cc` name, in a namespace labelled
  `gateway.0xc0.cc/internal: "true"`, without the `public` annotation; that
  name never goes on a `websecure` route.
- The namespace denies by default and its NetworkPolicies open only what it
  needs. A data service accepts connections and initiates none.

Render every component you touch exactly as Argo CD does, and validate what
comes out, with the tools in `mise.toml` (CI runs the same on every PR):

```
kustomize build --enable-helm platform/<component> \
  | kubeconform -strict -ignore-missing-schemas -kubernetes-version 1.36.0 -summary
```

An application renders the same way, one environment at a time:
`apps/<application>/prod`, `apps/<application>/staging`.

`kustomize` downloads the charts into `platform/<component>/charts/`, which
git ignores: nothing is vendored.
