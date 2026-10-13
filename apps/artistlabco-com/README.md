# artistlabco-com

The artist's site, the Node server of 0xc0-labs/artistlabco.com, a front of
Payload (operator decision, 2026-10-13; workspace#168). It is a BFF: the
browser reaches it alone, and it calls its environment's Payload at
`http://payload.<namespace>.svc` ([`../payload/README.md`](../payload/README.md),
Fronts). Two environments, each an Argo CD application of its own:

| | prod | staging |
|---|---|---|
| Application and namespace | `artistlabco-com` | `artistlabco-com-staging` |
| Address | `https://artistlabco.com`, public; `www.artistlabco.com` redirects there | `https://artistlabco-com-staging.int.0xc0.cc`, over WARP, and `https://staging.artistlabco.com`, public behind Cloudflare Access |
| Payload | `payload` | `payload-staging` |
| Replicas | 2, with a PodDisruptionBudget | 1 |
| Vault | `apps/artistlabco-com/*` | `apps/artistlabco-com-staging/*` |

Staging's public name sits behind a Cloudflare Access application, created in
`infrastructure`: a visitor gets in with an email in a group the operator
manages in Cloudflare's dashboard (how:
[`modules/access-app`](https://github.com/0xc0-labs/infrastructure/tree/main/modules/access-app)).
Its route also sends `X-Robots-Tag: noindex, nofollow`, in case Access is ever
misconfigured.

`base/` holds what both share; `prod/` and `staging/` add the namespace, the
routes, the Payload URL and its egress, and the pin. No volumes: it holds no
data, so there is no backup.

## Before the first sync: the secrets

The operator writes them as [`vault`'s README, Writing or rotating a
secret][vault-write] says, each environment at its own path:

| Path | Key | Value |
|---|---|---|
| `apps/<namespace>/payload` | `api_key` | Payload's API key for this front, made in its admin: read as `PAYLOAD_API_KEY` |
| `apps/<namespace>/preview` | `secret` | generated: `openssl rand -base64 32`, the same value as Payload's `apps/payload(-staging)/preview`: read as `PREVIEW_SECRET` |

OpenObserve's RUM token is `apps/shared/openobserve-rum`, and the image's pull
token `apps/shared/ghcr-pull`, since the package is private: both already
there. Vault needs no change: its `apps` role admits any namespace labelled
`vault.0xc0.cc/apps`.

## Upgrades

The pin is each environment's `kustomization.yaml`, written by the promoter
in 0xc0-labs/artistlabco.com: staging follows every release candidate
(`vX.Y.Z-rc.N`), and a release, run by hand on what staging runs, changes
prod's, whose merge is prod's approval. Both start as `v0.0.0` and an
all-zero digest: until the first rc and the first release, their syncs fail on
the image.

```yaml
images:
  - name: ghcr.io/0xc0-labs/artistlabco.com
    newTag: v0.1.0
    digest: sha256:<64 hex>
configMapGenerator:
  - name: release
    literals:
      - APP_VERSION=v0.1.0
      - APP_ENVIRONMENT=prod
```

Every signal carries the version and the environment, from the `release`
ConfigMap: `OTEL_RESOURCE_ATTRIBUTES`. To roll back, revert the PR: the
previous pin comes back with its digest.

## Headers

Traefik sets the baseline on every entrypoint
(`platform/traefik/security-headers.yaml`) and each overlay's route adds the
app's own (`security-headers` in `httproute.yaml`: no framing,
Permissions-Policy, COOP/CORP). The app sets its Content-Security-Policy.

## The way out

Default deny, and then: traces to the collector, DNS, its Payload's pods on
3000, and HTTPS to Cloudflare's ranges for `media.0xc0.cc`, which `next/image`
fetches (`base/network-policy.yaml`, `egress-media`: the ranges are
Cloudflare's, so the platform allows no narrower rule). Each route also
carries the browsers' RUM to OpenObserve.

[vault-write]: https://github.com/0xc0-labs/vault/blob/main/README.md#writing-or-rotating-a-secret
