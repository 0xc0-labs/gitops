# headlamp

Headlamp keeps its token login (`admin.yaml`). This component also holds the
ClusterRoleBinding `admins` (`admins.yaml`), which grants `cluster-admin` to
the group `admins`.

## A personal client certificate

An admin reaches the cluster with a client certificate whose organization is
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
their sign-in to Grafana and Argo CD at once, not this certificate.
