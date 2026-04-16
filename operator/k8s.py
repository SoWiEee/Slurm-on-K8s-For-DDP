"""Kubernetes API client.

Wraps the official Python SDK to provide Slurm-operator-specific helpers:
StatefulSet replica management, pod exec, and Slurm node drain/resume.
"""

from __future__ import annotations

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream as k8s_stream

from models import Config


class K8sClient:
    """Kubernetes API client using the official Python SDK.

    Uses in-cluster config when running inside a pod, falls back to
    the local kubeconfig for development.  Replaces the previous
    KubectlClient that shelled out to the kubectl binary.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        self._core = k8s_client.CoreV1Api()
        self._apps = k8s_client.AppsV1Api()

    def pod_is_ready(self, pod_name: str) -> bool:
        try:
            pod = self._core.read_namespaced_pod(pod_name, self.cfg.namespace)
            conditions = pod.status.conditions or []
            return any(c.type == "Ready" and c.status == "True" for c in conditions)
        except ApiException:
            return False

    # Backward-compatible alias.
    def pod_ready(self, pod_name: str) -> bool:
        return self.pod_is_ready(pod_name)

    def get_annotation(self, resource: str, name: str, key: str) -> str | None:
        """Return the value of a metadata annotation, or None if absent/unreadable."""
        try:
            if resource == "statefulset":
                obj = self._apps.read_namespaced_stateful_set(name, self.cfg.namespace)
            else:
                return None
            annotations = obj.metadata.annotations or {}
            return annotations.get(key)
        except ApiException:
            return None

    def set_annotation(self, resource: str, name: str, key: str, value: str) -> None:
        """Write a metadata annotation onto a resource (creates or overwrites)."""
        body = {"metadata": {"annotations": {key: value}}}
        if resource == "statefulset":
            self._apps.patch_namespaced_stateful_set(name, self.cfg.namespace, body)

    def get_ready_replicas(self, statefulset: str) -> int:
        """Return the number of Ready replicas for a StatefulSet (0 on error)."""
        try:
            sts = self._apps.read_namespaced_stateful_set(statefulset, self.cfg.namespace)
            return sts.status.ready_replicas or 0
        except ApiException:
            return 0

    def get_replicas(self, statefulset: str) -> int:
        """Return the desired replica count (spec.replicas) for a StatefulSet."""
        try:
            sts = self._apps.read_namespaced_stateful_set(statefulset, self.cfg.namespace)
            return sts.spec.replicas or 0
        except ApiException:
            return 0

    def patch_replicas(self, statefulset: str, replicas: int) -> None:
        """Patch spec.replicas on a StatefulSet via the K8s API."""
        body = {"spec": {"replicas": replicas}}
        self._apps.patch_namespaced_stateful_set(statefulset, self.cfg.namespace, body)

    def exec_in_controller(self, command: str) -> str:
        resp = k8s_stream(
            self._core.connect_get_namespaced_pod_exec,
            self.cfg.controller_pod,
            self.cfg.namespace,
            command=["bash", "-lc", command],
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )
        return resp.strip() if isinstance(resp, str) else ""

    def drain_slurm_node(self, node_name: str, reason: str = "operator-scale-down") -> None:
        """Mark a Slurm node DRAIN so no new jobs are scheduled onto it."""
        self.exec_in_controller(
            f"scontrol update NodeName={node_name} State=DRAIN Reason='{reason}' || true"
        )

    def resume_slurm_node(self, node_name: str) -> None:
        """Clear DRAIN state so the node can accept new jobs again."""
        self.exec_in_controller(
            f"scontrol update NodeName={node_name} State=RESUME || true"
        )

    def get_node_cpu_alloc(self, node_name: str) -> int:
        """Return the number of CPUs currently allocated on a node (0 = safe to remove)."""
        output = self.exec_in_controller(
            f"scontrol show node {node_name} 2>/dev/null"
            r" | grep -oP 'CPUAlloc=\K[0-9]+' || echo 0"
        )
        try:
            return int(output or "0")
        except ValueError:
            return 0
