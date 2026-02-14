#!/usr/bin/env bash
set -euo pipefail

kubectl -n slurm get pods -o wide
kubectl -n slurm exec statefulset/slurm-controller -- sinfo
kubectl -n slurm exec statefulset/slurm-controller -- scontrol show nodes
kubectl -n slurm exec statefulset/slurm-controller -- bash -lc 'ssh -o StrictHostKeyChecking=no slurm-worker-0.slurm-worker hostname'
