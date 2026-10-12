# Vault: initialise and unseal

Vault comes up sealed, and stays sealed after every restart of a pod, until it
is unsealed by hand (the workspace's
[`docs/design/platform.md`][design-platform]). The pods are not Ready while
sealed: Argo CD shows the Application as Progressing until then. Everything
below runs from a WARP client, through kubectl on a server:

```sh
# -t: the unseal prompt needs a terminal.
k() { ssh -t -i ~/.ssh/0xc0-homelab ops@10.10.4.21 \
  "sudo /var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml $*"; }
```

## Once: initialise

On `vault-0` only. It prints five unseal keys and the initial root token,
once and never again:

```sh
k -n vault exec vault-0 -- vault operator init -key-shares=5 -key-threshold=3
```

Keep the five keys and the root token **off the cluster and off the repos**:
in the operator's password manager, and a copy offline. Never in Vault
itself, nor in any repo, encrypted or not.

## After every restart: unseal

Three of the five keys, on every pod that shows `Sealed true`. Start with
`vault-0`; the others join its Raft cluster by themselves
(`retry_join`), then need their own three keys:

```sh
k -n vault exec -it vault-0 -- vault operator unseal   # three times, one key each
k -n vault exec -it vault-1 -- vault operator unseal   # likewise
k -n vault exec -it vault-2 -- vault operator unseal   # likewise
k -n vault exec vault-0 -- vault operator raft list-peers
```

`-it` makes Vault prompt for the key, so it never lands in the shell history.

## Then

- The root token is kept with the unseal keys, outside the cluster (operator
  decision, 2026-10-04): it is not revoked.
- Upgrades are by hand: the StatefulSet updates `OnDelete`. Delete one pod at
  a time, standbys first, and unseal each before the next. Before each delete,
  `vault operator raft list-peers` must show all three voters healthy.
- A consistent backup: `vault operator raft snapshot save` (PBS backs up the
  volumes too, crash-consistent).

The UI: <https://vault.int.0xc0.cc>, over WARP only.

[design-platform]: https://github.com/0xc0-labs/workspace/blob/main/docs/design/platform.md
