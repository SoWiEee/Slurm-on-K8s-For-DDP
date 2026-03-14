#!/usr/bin/env bash
set -euo pipefail

NAMESPACE=${NAMESPACE:-slurm}
CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}

kubectl config use-context "$KUBE_CONTEXT" >/dev/null
kubectl -n "$NAMESPACE" get pods -o wide
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-controller-0 --timeout=120s
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-worker-cpu-0 --timeout=120s

kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- sinfo
kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- scontrol show nodes
kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc 'ssh -o StrictHostKeyChecking=no slurm-worker-cpu-0.slurm-worker-cpu hostname'
