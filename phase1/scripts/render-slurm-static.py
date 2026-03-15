
#!/usr/bin/env python3
from __future__ import annotations
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CFG = ROOT / "phase1" / "manifests" / "worker-pools.json"
OUT = ROOT / "phase1" / "manifests" / "slurm-static.yaml"

def indent(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line if line else line for line in text.splitlines())

def build_slurm_conf(cfg: dict) -> tuple[str, str]:
    node_lines = []
    gres_lines = []
    partition_nodes = []
    gres_types = set()

    for pool in cfg["workerPools"]:
        name = pool["name"]
        svc = pool["service"]
        features = pool.get("features", [])
        gres = pool.get("gres", [])
        extra = []
        if features:
            extra.append(f"Feature={','.join(features)}")
        if gres:
            extra.append(f"Gres={','.join(gres)}")
            for g in gres:
                gres_types.add(g.split(":")[0])
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
            partition_nodes.append(node_name)
            for g in gres:
                gres_lines.append(f"NodeName={node_name} Name={g.split(':')[0]} Type={g.split(':')[1]} File=/dev/null")
    header = [
        "ClusterName=kind-slurm",
        "SlurmctldHost=slurm-controller-0(slurm-controller-0.slurm-controller.slurm.svc.cluster.local)",
        "MpiDefault=none",
        "ProctrackType=proctrack/linuxproc",
        "ReturnToService=2",
        "SlurmctldPidFile=/var/run/slurmctld.pid",
        "SlurmdPidFile=/var/run/slurmd.pid",
        "SlurmdSpoolDir=/var/spool/slurmd",
        "SlurmUser=root",
        "StateSaveLocation=/var/spool/slurmctld",
        "SwitchType=switch/none",
        "TaskPlugin=task/none",
        "SchedulerType=sched/backfill",
        "MailProg=/usr/bin/true",
        "SelectType=select/cons_tres",
        "SelectTypeParameters=CR_Core",
        "",
        "SlurmctldPort=6817",
        "SlurmdPort=6818",
        "AuthType=auth/munge",
        "CryptoType=crypto/munge",
    ]
    if gres_types:
        header.append(f"GresTypes={','.join(sorted(gres_types))}")
    header.append("")
    part = cfg["partition"]
    part_line = (
        f"PartitionName={part['name']} Nodes={','.join(partition_nodes)} "
        f"Default={'YES' if part.get('default', True) else 'NO'} "
        f"MaxTime={part.get('maxTime', 'INFINITE')} State={part.get('state', 'UP')}"
    )
    slurm_conf = "\n".join(header + node_lines + [part_line]) + "\n"
    gres_conf = "\n".join(gres_lines) + ("\n" if gres_lines else "")
    return slurm_conf, gres_conf

def main():
    cfg = json.loads(CFG.read_text())
    slurm_conf, gres_conf = build_slurm_conf(cfg)
    docs = []
    docs.append("""apiVersion: v1
kind: Namespace
metadata:
  name: slurm
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
    docs.append("""apiVersion: apps/v1
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
          image: slurm-controller:phase1
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
              exec slurmctld -Dvvv
          ports:
            - containerPort: 22
            - containerPort: 6817
          readinessProbe:
            exec:
              command: ["/bin/sh", "-c", "pgrep -x slurmctld >/dev/null && pgrep -x munged >/dev/null"]
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            exec:
              command: ["/bin/sh", "-c", "pgrep -x slurmctld >/dev/null"]
            initialDelaySeconds: 20
            periodSeconds: 20
          volumeMounts:
            - name: slurm-config
              mountPath: /etc/slurm
            - name: slurm-secrets
              mountPath: /slurm-secrets
              readOnly: true
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
---""")
    for pool in cfg["workerPools"]:
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
    spec:
      containers:
        - name: slurm-worker
          image: {pool['image']}
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
          volumeMounts:
            - name: slurm-config
              mountPath: /etc/slurm
            - name: slurm-secrets
              mountPath: /slurm-secrets
              readOnly: true
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
---""")
    OUT.write_text("\n".join(docs).rstrip() + "\n")
    print(f"Rendered {OUT} from {CFG}", flush=True)
    return 0

if __name__ == "__main__":
    main()
