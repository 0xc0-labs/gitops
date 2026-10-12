# offby1-cc

The offby1.cc landing page: the Node server of 0xc0-labs/offby1.cc, public
at `https://offby1.cc`; `www.offby1.cc` redirects there (`httproute.yaml`).
No volumes. Its one secret is OpenObserve's RUM client token,
`apps/shared/openobserve-rum`, key `client_token` (`vault-secrets.yaml`),
written as [`vault`'s README, Writing or rotating a secret][vault-write]
says.

## Upgrades

Every push to `main` in 0xc0-labs/offby1.cc is a release, `vX.Y.Z`, with
no staging and no release candidate ([`.github`'s README, Application
repos][github-apps]). Its PR here moves the pin alone, in
`kustomization.yaml`, and merging it is prod's approval; Argo CD rolls it
out one pod at a time, never below two ready (`maxUnavailable: 0`).

```yaml
images:
  - name: ghcr.io/0xc0-labs/offby1.cc
    newTag: v0.1.0
    digest: sha256:<64 hex>
configMapGenerator:
  - name: release
    literals:
      - APP_VERSION=v0.1.0
      - APP_ENVIRONMENT=prod
```

Until the first release, the pin is the `sha-<7>` tag the image job
publishes. Every signal carries the version and the environment, from the
`release` ConfigMap: `OTEL_RESOURCE_ATTRIBUTES`.

To roll back, revert the release's PR: the previous pin comes back, and with
it the previous digest.

The package on GHCR is public, so the cluster pulls with no secret.

## Headers

Traefik sets the baseline on every entrypoint
(`platform/traefik/security-headers.yaml`: HSTS, nosniff, Referrer-Policy)
and this route adds its own (`security-headers` in `httproute.yaml`: no
framing, Permissions-Policy, COOP/CORP). The app sets its own
Content-Security-Policy, with a nonce per request.

[vault-write]: https://github.com/0xc0-labs/vault/blob/main/README.md#writing-or-rotating-a-secret
[github-apps]: https://github.com/0xc0-labs/.github/blob/main/README.md#application-repos
