#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "manifests" / "core" / "worker-pools.json"
OUT = ROOT / "manifests" / "core" / "slurm-static.yaml"


def indent(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line if line else line for line in text.splitlines())


def build_slurm_conf(cfg: dict, real_gpu: bool = False) -> tuple[str, str]:
    node_lines: list[str] = []
    gres_lines: list[str] = []
    # partition_name → list of NodeName entries assigned to it
    partition_nodes: dict[str, list[str]] = {}
    gres_types: set[str] = set()

    for pool in cfg["workerPools"]:
        name = pool["name"]
        svc = pool["service"]
        features = pool.get("features", [])
        gres = pool.get("gres", [])
        # Each pool maps to exactly one partition. Pools without an explicit
        # `partition` field fall back to the first defined partition for
        # backwards compatibility, but worker-pools.json should always set it.
        pool_partition = pool.get("partition") or cfg.get("partitions", [{"name": "debug"}])[0]["name"]
        extra: list[str] = []
        if features:
            extra.append(f"Feature={','.join(features)}")
        if gres:
            extra.append(f"Gres={','.join(gres)}")
            for g in gres:
                gres_types.add(g.split(":")[0])

        # Keep the static maxNodes model for compatibility with the current
        # phase2 operator, which only scales StatefulSets and does not rewrite
        # slurm.conf on each scale event.
        for i in range(pool["maxNodes"]):
            node_name = f"{name}-{i}"
            node_addr = f"{node_name}.{svc}.slurm.svc.cluster.local"
            line = (
                f"NodeName={node_name} "
                f"NodeAddr={node_addr} "
                f"NodeHostname={node_name} "
                f"CPUs={pool['cpus']} RealMemory={pool['realMemory']} "
                f"Sockets={pool['sockets']} CoresPerSocket={pool['coresPerSocket']} "
                f"ThreadsPerCore={pool['threadsPerCore']} State=UNKNOWN"
            )
            if extra:
                line += " " + " ".join(extra)
            node_lines.append(line)
            partition_nodes.setdefault(pool_partition, []).append(node_name)
            for g in gres:
                parts = g.split(":")
                gres_name = parts[0]
                if gres_name == "mps":
                    # mps GRES in gres.conf has no Type and no File — only Count.
                    # With device-plugin sharing.mps, the plugin owns the daemon
                    # and exposes pre-sliced nvidia.com/gpu replicas; Slurm just
                    # tracks the SM% allocation.
                    count = parts[1] if len(parts) >= 2 else "100"
                    gres_lines.append(f"NodeName={node_name} Name=mps Count={count}")
                elif len(parts) >= 2:
                    # NVIDIA k8s device-plugin always exposes the allocated GPU
                    # at /dev/nvidia0 inside the container (regardless of host
                    # PCI index), so all real_gpu pools share the same File.
                    # Kind/dev: use /dev/null as a placeholder for scheduling.
                    device_file = "/dev/nvidia0" if real_gpu else "/dev/null"
                    count = parts[2] if len(parts) >= 3 else "1"
                    gres_lines.append(
                        f"NodeName={node_name} Name={gres_name} Type={parts[1]}"
                        f" Count={count} File={device_file}"
                    )

    header = [
        "ClusterName=kind-slurm",
        "SlurmctldHost=slurm-controller-0(slurm-controller-0.slurm-controller.slurm.svc.cluster.local)",
        "MpiDefault=pmi2",
        "ReturnToService=2",
        # Worker pods have no epilog scripts; setting CompleteWait=0 lets
        # slurmctld immediately finalize COMPLETING jobs without waiting for
        # an epilog-done signal. This prevents jobs from sticking in COMPLETING
        # when the pod is evicted during the (very short) epilog window.
        "CompleteWait=0",
        "SlurmctldPidFile=/var/run/slurmctld.pid",
        "SlurmdPidFile=/var/run/slurmd.pid",
        "SlurmdSpoolDir=/var/spool/slurmd",
        "SlurmUser=root",
        "StateSaveLocation=/var/spool/slurmctld",
        "SwitchType=switch/none",
        # GPU isolation is handled by the kubelet + libnvidia-container hook
        # (NVIDIA runtime, see manifests/gpu/runtime-class.yaml), not by Slurm.
        # Slurm 21.08 (Ubuntu 22.04 image) has incomplete cgroup v2 support
        # (no IgnoreSystemd, no CgroupAutomount=no), and freezer cgroup is not
        # mounted on cgroup-v2-only hosts (Ubuntu 24.04 default), so
        # task/cgroup + proctrack/cgroup fail at slurmd startup. Stay with
        # linuxproc + task/none even in real_gpu mode — Slurm's job is GRES
        # accounting and scheduling; resource isolation is layered below.
        "TaskPlugin=task/none",
        "ProctrackType=proctrack/linuxproc",
        "MailProg=/usr/bin/true",
        "SelectType=select/cons_tres",
        "SelectTypeParameters=CR_Core",
        "",
        "SlurmctldPort=6817",
        "SlurmdPort=6818",
        "AuthType=auth/munge",
        "CryptoType=crypto/munge",
        "AuthAltTypes=auth/jwt",
        "AuthAltParameters=jwt_key=/slurm-secrets/jwt_hs256.key",
        "",
        "# Job accounting via slurmdbd (gracefully degrades if slurmdbd is unavailable)",
        "AccountingStorageType=accounting_storage/slurmdbd",
        "AccountingStorageHost=slurmdbd.slurm.svc.cluster.local",
        "AccountingStoragePort=6819",
        "JobAcctGatherType=jobacct_gather/linux",
        "JobAcctGatherFrequency=30",
    ]
    if gres_types:
        header.append(f"GresTypes={','.join(sorted(gres_types))}")
        # Track GPU/MPS usage in slurmdbd so sacct/fairshare can see GRES alloc.
        header.append("AccountingStorageTRES=gres/gpu,gres/mps")
    header.append("")

    # Backwards compat: accept either `partitions` (list, multi-partition) or
    # the older single-partition `partition` block.
    partitions_cfg = cfg.get("partitions")
    if not partitions_cfg:
        single = cfg.get("partition", {"name": "debug", "default": True})
        partitions_cfg = [single]

    part_lines: list[str] = []
    for part in partitions_cfg:
        pname = part["name"]
        nodes = partition_nodes.get(pname, [])
        if not nodes:
            # Skip partitions with no nodes — slurmctld would fail to parse them.
            continue
        part_lines.append(
            f"PartitionName={pname} Nodes={','.join(nodes)} "
            f"Default={'YES' if part.get('default', False) else 'NO'} "
            f"MaxTime={part.get('maxTime', 'INFINITE')} State={part.get('state', 'UP')}"
        )
    slurm_conf = "\n".join(header + node_lines + part_lines) + "\n"
    gres_conf = "\n".join(gres_lines) + ("\n" if gres_lines else "")
    return slurm_conf, gres_conf


def main() -> int:
    parser = argparse.ArgumentParser(description="Render slurm-static.yaml from worker-pools.json")
    parser.add_argument(
        "--with-shared-storage",
        action="store_true",
        help="Include /shared NFS volumeMount and PVC volume in all StatefulSets",
    )
    parser.add_argument(
        "--with-lmod",
        action="store_true",
        help="Mount Lmod modulefile ConfigMaps into /opt/modulefiles on worker+login pods",
    )
    parser.add_argument(
        "--real-gpu",
        action="store_true",
        help=(
            "Real GPU mode (Linux host with NVIDIA device plugin). "
            "Sets gres.conf File=/dev/nvidia0, TaskPlugin=task/cgroup, "
            "and adds nvidia.com/gpu resource limits to GPU worker pods."
        ),
    )
    parser.add_argument(
        "--with-mps",
        action="store_true",
        help=(
            "Deprecated/no-op: MPS is now provided by the NVIDIA k8s device-plugin "
            "(`sharing.mps`). Worker pods no longer need hostIPC or a /tmp/nvidia-mps "
            "hostPath mount — the device-plugin owns the MPS daemon and exposes "
            "pre-sliced nvidia.com/gpu replicas. The flag is accepted for backwards "
            "compatibility with older bootstrap.sh invocations."
        ),
    )
    args = parser.parse_args()
    if args.with_mps and not args.real_gpu:
        print("--with-mps requires --real-gpu", file=sys.stderr)
        return 1
    if args.with_mps:
        print(
            "[render-core] note: --with-mps is a no-op; MPS is handled by the "
            "device-plugin sharing.mps config (manifests/gpu/nvidia-device-plugin.yaml).",
            file=sys.stderr,
        )

    # Snippets injected into every StatefulSet when --with-shared-storage is set.
    # The PVC claimName must match what phase3/manifests/shared-storage.yaml creates.
    shared_vm = (
        "\n            - name: shared-storage\n              mountPath: /shared"
        if args.with_shared_storage else ""
    )
    shared_vol = (
        "\n        - name: shared-storage\n          persistentVolumeClaim:\n            claimName: slurm-shared-rwx"
        if args.with_shared_storage else ""
    )
    # JWT key is added to the projected secrets volume so it lands at
    # /slurm-secrets/jwt_hs256.key — same path referenced in slurm.conf.
    # This avoids a subPath conflict with the ConfigMap mounted at /etc/slurm.
    jwt_projected_source = (
        "\n              - secret:"
        "\n                  name: slurm-jwt-secret"
        "\n                  items:"
        "\n                    - key: jwt_hs256.key"
        "\n                      path: jwt_hs256.key"
    )
    jwt_vm = ""
    jwt_vol = ""

    # Lmod: mount modulefile ConfigMaps into /opt/modulefiles/<family>/.
    # optional:true so pods start even when the ConfigMap hasn't been applied yet.
    _lmod_families = [
        ("openmpi",  "slurm-modulefile-openmpi"),
        ("python3",  "slurm-modulefile-python3"),
        ("cuda",     "slurm-modulefile-cuda"),
    ]
    if args.with_lmod:
        lmod_vms = "".join(
            f"\n            - name: modulefile-{fam}\n              mountPath: /opt/modulefiles/{fam}"
            for fam, _ in _lmod_families
        )
        lmod_vols = "".join(
            f"\n        - name: modulefile-{fam}"
            f"\n          configMap:"
            f"\n            name: {cm}"
            f"\n            optional: true"
            for fam, cm in _lmod_families
        )
    else:
        lmod_vms = ""
        lmod_vols = ""

    cfg = json.loads(CFG.read_text())
    slurm_conf, gres_conf = build_slurm_conf(cfg, real_gpu=args.real_gpu)
    docs: list[str] = []
    docs.append("""apiVersion: v1
kind: Namespace
metadata:
  name: slurm
---""")
    # PVC for slurmctld state — persists job queue and node state across pod restarts.
    # Uses the cluster's default StorageClass (local-path on Kind, gp2 on EKS, etc.).
    docs.append("""apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: slurm-ctld-state
  namespace: slurm
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
---""")
    docs.append("""apiVersion: v1
kind: Service
metadata:
  name: slurm-controller
  namespace: slurm
spec:
  clusterIP: None
  publishNotReadyAddresses: true
  selector:
    app: slurm-controller
  ports:
    - name: slurmctld
      port: 6817
      targetPort: 6817
---""")
    docs.append("""apiVersion: v1
kind: Service
metadata:
  name: slurm-restapi
  namespace: slurm
spec:
  selector:
    app: slurm-controller
  ports:
    - name: rest
      port: 6820
      targetPort: 6820
---""")
    for pool in cfg["workerPools"]:
        docs.append(f"""apiVersion: v1
kind: Service
metadata:
  name: {pool['service']}
  namespace: slurm
spec:
  clusterIP: None
  publishNotReadyAddresses: true
  selector:
    app: {pool['appLabel']}
  ports:
    - name: slurmd
      port: 6818
      targetPort: 6818
---""")
    cm = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: slurm-config\n  namespace: slurm\ndata:\n  slurm.conf: |\n"
    cm += indent(slurm_conf.rstrip("\n"), 4) + "\n"
    if gres_conf:
        cm += "  gres.conf: |\n" + indent(gres_conf.rstrip("\n"), 4) + "\n"
    cm += "---"
    docs.append(cm)
    docs.append(f"""apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: slurm-controller
  namespace: slurm
spec:
  serviceName: slurm-controller
  replicas: 1
  selector:
    matchLabels:
      app: slurm-controller
  template:
    metadata:
      labels:
        app: slurm-controller
    spec:
      containers:
        - name: slurm-controller
          image: slurm-controller:latest
          imagePullPolicy: IfNotPresent
          command:
            - /bin/bash
            - -lc
            - |
              set -euo pipefail
              MUNGE_SRC=/slurm-secrets/munge.key
              MUNGE_DST=/etc/munge/munge.key
              mkdir -p /etc/munge /run/munge /var/lib/munge /var/log/munge /run/sshd
              chown -R munge:munge /etc/munge /run/munge /var/lib/munge /var/log/munge
              chmod 0700 /etc/munge /var/lib/munge /var/log/munge
              chmod 0711 /run/munge
              install -o munge -g munge -m 0400 "$MUNGE_SRC" "$MUNGE_DST"
              ssh-keygen -A >/dev/null 2>&1 || true
              /usr/sbin/sshd
              su -s /bin/sh -c '/usr/sbin/munged --syslog' munge
              sleep 1
              pgrep -x munged >/dev/null
              # Wait for slurmdbd if accounting is configured, so slurmctld
              # does not fatal-exit on first boot due to missing TRES data.
              if grep -q 'AccountingStorageType=accounting_storage/slurmdbd' /etc/slurm/slurm.conf 2>/dev/null; then
                echo "[controller] waiting for slurmdbd port 6819..."
                until (echo >/dev/tcp/slurmdbd.slurm.svc.cluster.local/6819) 2>/dev/null; do
                  sleep 3
                done
                echo "[controller] slurmdbd is up"
              fi
              exec slurmctld -Dvvv &
              CTLD_PID=$!
              # Wait for slurmctld to be ready before starting slurmrestd.
              # slurmrestd needs SLURM_JWT set (from scontrol token) so that its
              # rest_auth/jwt plugin can bootstrap a valid auth context.
              until scontrol ping >/dev/null 2>&1; do sleep 2; done
              # lifespan=315360000 (10 years) avoids the daemon losing its auth
              # context after the default 24-hour token expiry.
              SLURM_JWT=$(scontrol token username=root lifespan=315360000 | sed 's/SLURM_JWT=//') \
                SLURMRESTD_SECURITY=disable_user_check \
                /usr/sbin/slurmrestd -a rest_auth/jwt -s openapi/v0.0.37 0.0.0.0:6820 &
              wait $CTLD_PID
          ports:
            - containerPort: 22
            - containerPort: 6817
            - containerPort: 6820
          readinessProbe:
            exec:
              command: ["/bin/sh", "-c", "pgrep -x slurmctld >/dev/null && pgrep -x munged >/dev/null"]
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            exec:
              command: ["/bin/sh", "-c", "pgrep -x slurmctld >/dev/null && pgrep -x slurmrestd >/dev/null"]
            initialDelaySeconds: 60
            periodSeconds: 20
          volumeMounts:
            - name: slurm-config
              mountPath: /etc/slurm
            - name: slurm-secrets
              mountPath: /slurm-secrets
              readOnly: true
            - name: ctld-state
              mountPath: /var/spool/slurmctld{jwt_vm}{shared_vm}
      volumes:
        - name: slurm-config
          configMap:
            name: slurm-config
        - name: slurm-secrets
          projected:
            sources:
              - secret:
                  name: slurm-munge-key
                  items:
                    - key: munge.key
                      path: munge.key
              - secret:
                  name: slurm-ssh-key
                  items:
                    - key: id_ed25519
                      path: id_ed25519
                    - key: id_ed25519.pub
                      path: id_ed25519.pub{jwt_projected_source}
        - name: ctld-state
          persistentVolumeClaim:
            claimName: slurm-ctld-state{shared_vol}
---""")
    # PDB: prevent voluntary eviction of the single slurmctld pod.
    docs.append("""apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: slurm-controller-pdb
  namespace: slurm
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: slurm-controller
---""")
    for pool in cfg["workerPools"]:
        is_gpu_pool = any(g.startswith("gpu") for g in pool.get("gres", []))
        has_mps_gres = any(g.startswith("mps") for g in pool.get("gres", []))
        # GPU resource limits: injected when --real-gpu and pool has GPU GRES.
        # The NVIDIA device plugin exposes nvidia.com/gpu; the count comes from
        # the first "gpu:*:N" entry in the pool's gres list.
        gpu_count = "1"
        if is_gpu_pool and args.real_gpu:
            for g in pool.get("gres", []):
                parts = g.split(":")
                if parts[0] == "gpu" and len(parts) >= 3:
                    gpu_count = parts[2]
                    break
        gpu_resources = (
            f"\n          resources:\n            limits:\n              nvidia.com/gpu: \"{gpu_count}\""
            f"\n            requests:\n              nvidia.com/gpu: \"{gpu_count}\""
            if is_gpu_pool and args.real_gpu else ""
        )
        # MPS via device-plugin sharing.mps: no hostIPC, no /tmp/nvidia-mps
        # hostPath mount needed on the worker pod. The device-plugin runs the
        # MPS daemon itself and injects CUDA_MPS_* env vars + NVIDIA_VISIBLE_*
        # into pods that request `nvidia.com/gpu`. We keep these placeholders
        # empty for now; if a future deployment wants to revert to a self-hosted
        # MPS DaemonSet, this is the splice point.
        _ = has_mps_gres
        mps_vm = ""
        mps_vol = ""
        mps_host_ipc = ""
        # Pods that request nvidia.com/gpu must run under the NVIDIA container
        # runtime so the libnvidia-container hook injects /dev/nvidia*. k3s
        # 1.27+ auto-registers the `nvidia` runtime; we apply a RuntimeClass
        # named `nvidia` (manifests/gpu/runtime-class.yaml) and reference it
        # here. CPU-only pools and Kind dev mode skip this.
        runtime_class = (
            "\n      runtimeClassName: nvidia"
            if is_gpu_pool and args.real_gpu else ""
        )
        docs.append(f"""apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {pool['name']}
  namespace: slurm
spec:
  serviceName: {pool['service']}
  replicas: {pool['replicas']}
  selector:
    matchLabels:
      app: {pool['appLabel']}
  template:
    metadata:
      labels:
        app: {pool['appLabel']}
        worker-class: {pool['workerClass']}
    spec:{runtime_class}{mps_host_ipc}
      containers:
        - name: slurm-worker
          image: {pool['image']}
          imagePullPolicy: IfNotPresent{gpu_resources}
          command:
            - /bin/bash
            - -lc
            - |
              set -euo pipefail
              MUNGE_SRC=/slurm-secrets/munge.key
              MUNGE_DST=/etc/munge/munge.key
              mkdir -p /etc/munge /run/munge /var/lib/munge /var/log/munge /run/sshd
              chown -R munge:munge /etc/munge /run/munge /var/lib/munge /var/log/munge
              chmod 0700 /etc/munge /var/lib/munge /var/log/munge
              chmod 0711 /run/munge
              install -o munge -g munge -m 0400 "$MUNGE_SRC" "$MUNGE_DST"
              ssh-keygen -A >/dev/null 2>&1 || true
              /usr/sbin/sshd
              su -s /bin/sh -c '/usr/sbin/munged --syslog' munge
              sleep 1
              pgrep -x munged >/dev/null
              # Clear any stale DRAIN set by the previous pod's preStop hook.
              # The hook drains the node before termination; when a new pod starts
              # for the same StatefulSet ordinal, the DRAIN persists in slurmctld
              # state and blocks new jobs. ReturnToService=2 clears DOWN but not
              # DRAIN, so we clear it explicitly here while munge is already up.
              scontrol update nodename="$(hostname)" state=resume 2>/dev/null || true
              exec slurmd -Dvvv -N "$(hostname)"
          ports:
            - containerPort: 22
            - containerPort: 6818
          readinessProbe:
            exec:
              command: ["/bin/sh", "-c", "pgrep -x slurmd >/dev/null && pgrep -x munged >/dev/null"]
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            exec:
              command: ["/bin/sh", "-c", "pgrep -x slurmd >/dev/null"]
            initialDelaySeconds: 20
            periodSeconds: 20
          lifecycle:
            preStop:
              exec:
                command:
                  - /bin/sh
                  - -c
                  - >-
                    scontrol update nodename=$(hostname) state=drain reason=k8s-eviction
                    2>/dev/null || true; sleep 10
          volumeMounts:
            - name: slurm-config
              mountPath: /etc/slurm
            - name: slurm-secrets
              mountPath: /slurm-secrets
              readOnly: true{shared_vm}{lmod_vms}{mps_vm}
      volumes:
        - name: slurm-config
          configMap:
            name: slurm-config
        - name: slurm-secrets
          projected:
            sources:
              - secret:
                  name: slurm-munge-key
                  items:
                    - key: munge.key
                      path: munge.key
              - secret:
                  name: slurm-ssh-key
                  items:
                    - key: id_ed25519
                      path: id_ed25519
                    - key: id_ed25519.pub
                      path: id_ed25519.pub{shared_vol}{lmod_vols}{mps_vol}
---""")
        # PDB: at most 1 worker voluntarily disrupted at a time per pool.
        docs.append(f"""apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {pool['name']}-pdb
  namespace: slurm
spec:
  maxUnavailable: 1
  selector:
    matchLabels:
      app: {pool['appLabel']}
---""")
    docs.append(f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: slurm-login
  namespace: slurm
spec:
  replicas: 1
  selector:
    matchLabels:
      app: slurm-login
  template:
    metadata:
      labels:
        app: slurm-login
    spec:
      containers:
        - name: slurm-login
          image: slurm-worker:latest
          imagePullPolicy: IfNotPresent
          command:
            - /bin/bash
            - -lc
            - |
              set -euo pipefail
              MUNGE_SRC=/slurm-secrets/munge.key
              MUNGE_DST=/etc/munge/munge.key
              SSH_SRC_DIR=/slurm-secrets
              if [[ ! -f "$MUNGE_SRC" ]]; then
                echo "[login] missing $MUNGE_SRC" >&2
                exit 1
              fi
              mkdir -p /etc/munge /run/munge /var/lib/munge /var/log/munge /run/sshd
              chown -R munge:munge /etc/munge /run/munge /var/lib/munge /var/log/munge
              chmod 0700 /etc/munge /var/lib/munge /var/log/munge
              chmod 0711 /run/munge
              cp "$MUNGE_SRC" "$MUNGE_DST"
              chown munge:munge "$MUNGE_DST"
              chmod 0400 "$MUNGE_DST"
              install -d -m 0700 /root/.ssh
              if [[ -f "$SSH_SRC_DIR/id_ed25519" ]]; then
                cp "$SSH_SRC_DIR/id_ed25519" /root/.ssh/id_ed25519
                chmod 0600 /root/.ssh/id_ed25519
              fi
              if [[ -f "$SSH_SRC_DIR/id_ed25519.pub" ]]; then
                cp "$SSH_SRC_DIR/id_ed25519.pub" /root/.ssh/id_ed25519.pub
                chmod 0644 /root/.ssh/id_ed25519.pub
                cat /root/.ssh/id_ed25519.pub >> /root/.ssh/authorized_keys
                chmod 0600 /root/.ssh/authorized_keys
              fi
              ssh-keygen -A >/dev/null 2>&1 || true
              if [[ -d /opt/slurm-runtime-src ]]; then
                install -d -m 0755 /opt/slurm-runtime
                cp -f /opt/slurm-runtime-src/* /opt/slurm-runtime/ 2>/dev/null || true
                chmod +x /opt/slurm-runtime/* 2>/dev/null || true
              fi
              if ! su -s /bin/sh -c '/usr/sbin/munged --syslog' munge; then
                echo "[login] munged failed to start" >&2
                exit 1
              fi
              sleep 1
              pgrep -x munged >/dev/null
              /usr/sbin/sshd
              echo "[login] ready: munged+sshd running"
              tail -f /dev/null
          readinessProbe:
            exec:
              command: ["/bin/sh", "-c", "pgrep -x munged >/dev/null"]
            initialDelaySeconds: 5
            periodSeconds: 10
          volumeMounts:
            - name: slurm-config
              mountPath: /etc/slurm
            - name: slurm-secrets
              mountPath: /slurm-secrets
              readOnly: true
            - name: slurm-ddp-runtime
              mountPath: /opt/slurm-runtime-src
              readOnly: true{shared_vm}{lmod_vms}
      volumes:
        - name: slurm-config
          configMap:
            name: slurm-config
        - name: slurm-secrets
          projected:
            sources:
              - secret:
                  name: slurm-munge-key
                  items:
                    - key: munge.key
                      path: munge.key
              - secret:
                  name: slurm-ssh-key
                  items:
                    - key: id_ed25519
                      path: id_ed25519
                    - key: id_ed25519.pub
                      path: id_ed25519.pub
        - name: slurm-ddp-runtime
          configMap:
            name: slurm-ddp-runtime
            defaultMode: 0755{shared_vol}{lmod_vols}
---""")
    # PDB: allow at most 1 login pod to be voluntarily disrupted.
    docs.append("""apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: slurm-login-pdb
  namespace: slurm
spec:
  maxUnavailable: 1
  selector:
    matchLabels:
      app: slurm-login
---""")
    docs.append("""apiVersion: v1
kind: Service
metadata:
  name: slurm-login
  namespace: slurm
spec:
  selector:
    app: slurm-login
  ports:
    - name: ssh
      port: 22
      targetPort: 22""")
    OUT.write_text("\n".join(docs).rstrip() + "\n")
    print(f"Rendered {OUT} from {CFG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
