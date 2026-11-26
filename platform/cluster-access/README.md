# cluster-access

How people are cluster-admin. The ClusterRoleBinding `admins` (`admins.yaml`)
grants `cluster-admin` to the group `admins`, which the API server gives to
every Cloudflare Access login and a client certificate carries as its
organization. It is a component of its own so that removing
another one, Headlamp for one, does not take the admins' access with it.

## Signing in with kubectl

`kubectl` signs in through Cloudflare Access, the way Headlamp, Grafana and
Argo CD do: no token or certificate to make. It needs the plugin
`kubectl oidc-login` ([kubelogin](https://github.com/int128/kubelogin)), which
`mise.toml` declares with `kubectl`, both pinned.

```sh
mise install
```

The kubeconfig keeps its cluster and context, with a user that runs the
plugin. `<issuer>` and `<client id>` are Access's OIDC application: the issuer
is `https://0xc0.cloudflareaccess.com/cdn-cgi/access/sso/oidc/<client id>`.

```sh
kubectl config set-credentials <email> \
  --exec-api-version=client.authentication.k8s.io/v1 \
  --exec-interactive-mode=IfAvailable \
  --exec-command=kubectl \
  --exec-arg=oidc-login \
  --exec-arg=get-token \
  --exec-arg=--oidc-issuer-url=<issuer> \
  --exec-arg=--oidc-client-id=<client id> \
  --exec-arg=--oidc-pkce-method=S256 \
  --exec-arg=--oidc-extra-scope=email
kubectl config set-context <cluster> --cluster=<cluster> --user=<email>
kubectl config use-context <cluster>
```

- **No client secret.** For `kubectl` the application is a public client:
  PKCE stands where the secret would, and a secret in every admin's kubeconfig
  would be no secret. Headlamp is the confidential client and keeps its own
  (`platform/headlamp/`).
- **PKCE is forced.** Access's discovery document does not advertise
  `code_challenge_methods_supported`, and the plugin picks PKCE from that list
  by default: left to detection it would send none and Access would refuse the
  login. `--oidc-pkce-method=S256` skips the detection.
- **`email`** is an extra scope because the plugin asks only for `openid`, and
  the API server takes the username from the token's `email`.

The first command that needs a token opens the browser at Cloudflare. Sign in
with GitHub or the one-time PIN, and the command goes on. The plugin listens
on `localhost:8000`, or `18000` when that is taken, the two redirect URIs the
application allows. Then:

```sh
kubectl auth whoami
```

shows the person's email as the username and `admins` among the groups.

The token is cached in `~/.kube/cache/oidc-login/` and renewed on its own.
Signing out is deleting it:

```sh
kubectl oidc-login clean
```

If Cloudflare Access is down, the client certificate below still works.

## A personal client certificate

The fallback when Cloudflare Access is not there. An admin reaches the
cluster with a client certificate whose organization is
`admins` and whose common name is their GitHub user. It lasts 30 days.

```sh
openssl genrsa -out me.key 4096
openssl req -new -key me.key -subj "/O=admins/CN=<github user>" -out me.csr

kubectl apply -f - <<YAML
apiVersion: certificates.k8s.io/v1
kind: CertificateSigningRequest
metadata:
  name: <github user>
spec:
  request: $(base64 < me.csr | tr -d '\n')
  signerName: kubernetes.io/kube-apiserver-client
  expirationSeconds: 2592000
  usages: [client auth]
YAML
```

An existing admin approves it, then the requester reads the certificate:

```sh
kubectl certificate approve <github user>
kubectl get csr <github user> -o jsonpath='{.status.certificate}' | base64 -d > me.crt
```

The kubeconfig, with the cluster's server and CA from an existing one:

```sh
kubectl config set-credentials <github user> \
  --client-certificate=me.crt --client-key=me.key --embed-certs=true
kubectl config set-context <cluster> --cluster=<cluster> --user=<github user>
kubectl config use-context <cluster>
```

Revoking is not renewing: Kubernetes cannot revoke a client certificate, so
access ends when the 30 days do. Removing someone from the GitHub team stops
their sign-in to Grafana, Argo CD, Headlamp and `kubectl` at once, not this
certificate.
