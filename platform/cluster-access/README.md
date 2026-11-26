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

The kubeconfig needs three entries: a cluster, a user that runs the plugin,
and a context that joins them. Each command below is one line, to paste as it
is.

**The cluster.** Someone who already has an entry for this cluster in their
kubeconfig skips this step. The API server is reached over WARP at
`https://10.10.4.10:6443`: `10.10.4.10` is the load balancers' virtual
address, `platform_vip` in infrastructure's
`ansible/inventory/group_vars/platform.yml`, and the API servers' certificate
names it. The certificate authority is on each server
(`vm-rke2-01` to `-03`, `10.10.4.21` to `.23`), as
`/var/lib/rancher/rke2/server/tls/server-ca.crt`. It is a public certificate,
not a secret. Copy it over SSH into a temporary file (an admin without SSH
access asks one who has it for the file), embed it, and delete the file:

```sh
ssh -i ~/.ssh/0xc0-homelab ops@10.10.4.21 'sudo cat /var/lib/rancher/rke2/server/tls/server-ca.crt' > /tmp/0xc0-ca.crt
mise exec -- kubectl config set-cluster 0xc0 --server=https://10.10.4.10:6443 --certificate-authority=/tmp/0xc0-ca.crt --embed-certs=true
rm /tmp/0xc0-ca.crt
```

**The user.** It runs the plugin. The issuer and the client ID are Access's
OIDC application, neither a secret: the issuer is
`https://0xc0.cloudflareaccess.com/cdn-cgi/access/sso/oidc/<client id>`.

```sh
mise exec -- kubectl config set-credentials oidc --exec-api-version=client.authentication.k8s.io/v1 --exec-interactive-mode=IfAvailable --exec-command=kubectl --exec-arg=oidc-login --exec-arg=get-token --exec-arg=--oidc-issuer-url=https://0xc0.cloudflareaccess.com/cdn-cgi/access/sso/oidc/ce6ce5b6752cc8cd7366367c36b3f8cea85e4feff9d16de741a16ce219fb1640 --exec-arg=--oidc-client-id=ce6ce5b6752cc8cd7366367c36b3f8cea85e4feff9d16de741a16ce219fb1640 --exec-arg=--oidc-pkce-method=S256 --exec-arg=--oidc-extra-scope=email --exec-arg=--oidc-extra-scope=offline_access
```

Someone who set the entry before runs this `set-credentials` command again:
it replaces the user's arguments, and the new scope reaches only a sign-in
made after it.

**The context** joins the two and becomes the current one:

```sh
mise exec -- kubectl config set-context 0xc0-oidc --cluster=0xc0 --user=oidc
mise exec -- kubectl config use-context 0xc0-oidc
```

- **No client secret.** For `kubectl` the application is a public client:
  PKCE stands where the secret would, and a secret in every admin's kubeconfig
  would be no secret. Headlamp signs in through the same application and
  holds its client secret in `platform/headlamp/`, but Access requires PKCE of
  every request to a public client, so Headlamp sends one too.
- **PKCE is forced.** Access's discovery document does not advertise
  `code_challenge_methods_supported`, and the plugin picks PKCE from that list
  by default: left to detection it would send none and Access would refuse the
  login. `--oidc-pkce-method=S256` skips the detection. Headlamp has
  `usePKCE` on for the same reason.
- **`email`** is an extra scope because the plugin asks only for `openid`, and
  the API server takes the username from the token's `email`.
- **`offline_access`** asks Access for a refresh token. The plugin then renews
  the one-hour token without the browser, for a day, and the browser opens
  again only after that.

The first command that needs a token opens the browser at Cloudflare. Sign in
with GitHub or the one-time PIN, and the command goes on. The plugin listens
on `localhost:8000`, or `18000` when that is taken, the two redirect URIs the
application allows. Then:

If the browser is closed, or the command interrupted, before the sign-in ends,
`kubectl` fails with "context canceled": running it again is enough.

```sh
mise exec -- kubectl auth whoami
```

prints the person's email as the username and the groups `admins` and
`system:authenticated`.

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
