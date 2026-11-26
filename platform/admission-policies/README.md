# admission-policies

Kubernetes places every application pod on the `apps` node pool itself
(workspace#301, gitops#696). Before, each application listed
`apps/_components/pool-apps`, which patched its workloads' manifests: an
application that forgot it could land on the servers (`infra` pool). Now the
API server decides, from one label on the namespace.

Cluster-scoped resources only, so the Application's namespace stays empty.
Needs Kubernetes 1.36 (`admissionregistration.k8s.io/v1` serves both kinds).

## The label

A namespace whose pods belong on `apps` carries `0xc0.cc/pool: apps`:
`offby1-cc`, `artistlabco-com`, `artistlabco-com-staging`,
`mautic-artistlabco-com`, `plane`, `matchmark` and `matchmark-sandboxes`
(its `egress-proxy` runs on `apps`; the sandbox pods are gVisor, below). The
label sits in the Namespace manifest, in git.

## What the policies do

**`pool-apps-placement`**, a `MutatingAdmissionPolicy`. On Pod `CREATE` in a
labelled namespace it adds `nodeSelector: {0xc0.cc/pool: apps}` and the
toleration `0xc0.cc/pool=apps:NoSchedule` when the pod lacks them. It adds and
never overwrites: a pod that names another pool is the guard's to reject.

**`pool-apps-guard`**, a `ValidatingAdmissionPolicy` with `Deny`. Mutation runs
first, so a pod the placement fixed passes. It rejects:

- in a labelled namespace, a pod whose `nodeSelector` names a pool other than
  `apps`, or whose required node affinity on `0xc0.cc/pool` names another;
- in any other namespace, a pod that selects `apps` through `nodeSelector` or
  required node affinity.

Each message names the namespace and says why. Pods with `runtimeClassName:
gvisor` are skipped by both (a `matchCondition`): the RuntimeClass places them
on `sandbox`, and the RuntimeClass admission rejects a `nodeSelector` that
disagrees. DaemonSet pods are unaffected: they select nodes by name, and
tolerating the `apps` taint is not a selection.

The guard has `failurePolicy: Ignore` because it covers every namespace: a
fault in an expression must never stop a system pod. The placement is scoped
to labelled namespaces and fails closed.

## Adding an application namespace

Label its Namespace `0xc0.cc/pool: apps`. Nothing else: no component, no
patch. Until `apps/_components/pool-apps` is removed (gitops#697), an
application that lists it still sets the same values itself, which the policy
leaves as they are.

A namespace that must not run on `apps` stays unlabelled: its pods cannot
select the pool.

## Order

The label is applied by the application's own Application, these policies by
`admission-policies`. A pod created in a namespace before its label is synced
and that selects `apps` (through `pool-apps`) is denied until it is: the
retry of the sync, or the pod's controller, settles it.
