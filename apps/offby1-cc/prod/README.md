# offby1-cc

The offby1.cc landing page: the Node server of 0xc0-labs/offby1.cc, public
at `https://offby1.cc`; `www.offby1.cc` redirects there (`httproute.yaml`).
No volumes. Its one secret is OpenObserve's RUM client token, from Vault
(`vault-secrets.yaml`).

## Upgrades

Every push to `main` in 0xc0-labs/offby1.cc publishes
`ghcr.io/0xc0-labs/offby1.cc` as `sha-<7>` and `main`, and its
`container-image` job prints the image as `tag@digest` in its summary. An
upgrade is that string in `deployment.yaml`, in a PR here; Argo CD rolls it
out one pod at a time, never below two ready (`maxUnavailable: 0`).

The package on GHCR is public, so the cluster pulls with no secret.

## Headers

Traefik sets the baseline on every entrypoint
(`platform/traefik/security-headers.yaml`: HSTS, nosniff, Referrer-Policy)
and this route adds its own (`security-headers` in `httproute.yaml`: no
framing, Permissions-Policy, COOP/CORP). The app sets its own
Content-Security-Policy, with a nonce per request.
