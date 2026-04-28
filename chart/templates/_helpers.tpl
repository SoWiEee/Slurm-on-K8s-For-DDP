{{/*
=============================================================================
slurm-platform helper templates
=============================================================================
Stage A scope:
  - slurm-platform.slurmConf      — full slurm.conf body (header + nodes + parts)
  - slurm-platform.gresConf       — gres.conf body (empty when no pool has gres)
  - slurm-platform.partitionsJson — PARTITIONS_JSON for the elastic operator
  - slurm-platform.gresList       — pool.gres normalized to ["name:type:count", ...]
                                    or ["name:count", ...] for typeless (mps)
  - slurm-platform.gresTypes      — sorted, comma-joined unique gres names
  - slurm-platform.realGpu        — true when cluster.runtime == "k3s" AND
                                    any pool has a non-mps gres entry. Drives
                                    gres.conf File path (/dev/nvidia0 vs /dev/null)

These named templates exist to replace `scripts/render-core.py::build_slurm_conf`
and produce byte-equivalent slurm.conf / gres.conf so we can diff helm output
against the current `manifests/core/slurm-static.yaml` ConfigMap.

Standard chart name / labels helpers follow.
*/}}

{{/* Chart name (truncated to 63 chars per K8s label limit). */}}
{{- define "slurm-platform.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully qualified release name. */}}
{{- define "slurm-platform.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Chart label (chart name + version). */}}
{{- define "slurm-platform.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Common labels applied to all resources. */}}
{{- define "slurm-platform.labels" -}}
helm.sh/chart: {{ include "slurm-platform.chart" . }}
{{ include "slurm-platform.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "slurm-platform.selectorLabels" -}}
app.kubernetes.io/name: {{ include "slurm-platform.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
-----------------------------------------------------------------------------
slurm-platform.gresList — normalize pool.gres to a list of canonical strings.

Accepts both inline strings ("gpu:rtx4070:1", "mps:100") and structured
form ({name, type, count}). Returns a list like ["gpu:rtx4070:1", "mps:100"]
for direct concatenation into slurm.conf `Gres=...`.

Pass the pool dict as the context.
-----------------------------------------------------------------------------
*/}}
{{- define "slurm-platform.gresList" -}}
{{- $out := list -}}
{{- range .gres -}}
  {{- if kindIs "string" . -}}
    {{- $out = append $out . -}}
  {{- else if kindIs "map" . -}}
    {{- if .type -}}
      {{- $out = append $out (printf "%s:%s:%v" .name .type (default 1 .count)) -}}
    {{- else -}}
      {{- $out = append $out (printf "%s:%v" .name (default 1 .count)) -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{/*
-----------------------------------------------------------------------------
slurm-platform.gresTypes — sorted unique list of gres "name" prefixes across
all pools, joined by comma. Empty string if no gres anywhere.

Example output: "gpu,mps"
-----------------------------------------------------------------------------
*/}}
{{- define "slurm-platform.gresTypes" -}}
{{- $names := list -}}
{{- range .Values.pools -}}
  {{- range .gres -}}
    {{- if kindIs "string" . -}}
      {{- $parts := splitList ":" . -}}
      {{- $names = append $names (index $parts 0) -}}
    {{- else if kindIs "map" . -}}
      {{- $names = append $names .name -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- $names | uniq | sortAlpha | join "," -}}
{{- end -}}

{{/*
-----------------------------------------------------------------------------
slurm-platform.realGpu — "true" / "" depending on whether gres.conf entries
should use /dev/nvidia0 (k3s + real GPU) or /dev/null (Kind dev placeholder).

Mirrors render-core.py's --real-gpu flag. Currently keyed on cluster.runtime,
which matches how scripts/bootstrap.sh decides today.
-----------------------------------------------------------------------------
*/}}
{{- define "slurm-platform.realGpu" -}}
{{- if eq .Values.cluster.runtime "k3s" -}}true{{- end -}}
{{- end -}}

{{/*
-----------------------------------------------------------------------------
slurm-platform.slurmConf — full slurm.conf body.

Layout (matches render-core.py::build_slurm_conf line-by-line so the produced
ConfigMap can be diffed against manifests/core/slurm-static.yaml):

  ClusterName / SlurmctldHost / MpiDefault / ReturnToService / CompleteWait
  PidFile / SpoolDir / SlurmUser / StateSaveLocation / SwitchType
  TaskPlugin / ProctrackType / MailProg / SelectType / SelectTypeParameters
  <blank>
  SlurmctldPort / SlurmdPort
  AuthType / CryptoType / AuthAltTypes / AuthAltParameters
  <blank>
  # Job accounting via slurmdbd
  AccountingStorage* + JobAcctGather*
  GresTypes (if any pool has gres)
  AccountingStorageTRES (if any pool has gres AND slurm.accounting.storageTres set)
  <blank>
  NodeName lines (in pool order, ordinal 0..maxNodes-1)
  PartitionName lines (in partition order, skipping empty partitions)
-----------------------------------------------------------------------------
*/}}
{{- define "slurm-platform.slurmConf" -}}
{{- $ns := .Values.cluster.namespace -}}
{{- $s := .Values.slurm -}}
ClusterName={{ .Values.cluster.name }}
SlurmctldHost={{ $s.controllerHost }}({{ $s.controllerHost }}.{{ $s.controllerService }}.{{ $ns }}.svc.cluster.local)
MpiDefault={{ $s.mpiDefault }}
ReturnToService={{ $s.returnToService }}
CompleteWait={{ $s.completeWait }}
SlurmctldPidFile={{ $s.slurmctldPidFile }}
SlurmdPidFile={{ $s.slurmdPidFile }}
SlurmdSpoolDir={{ $s.slurmdSpoolDir }}
SlurmUser={{ $s.slurmUser }}
StateSaveLocation={{ $s.stateSaveLocation }}
SwitchType={{ $s.switchType }}
TaskPlugin={{ $s.taskPlugin }}
ProctrackType={{ $s.proctrackType }}
MailProg={{ $s.mailProg }}
SelectType={{ $s.selectType }}
SelectTypeParameters={{ $s.selectTypeParameters }}

SlurmctldPort={{ $s.slurmctldPort }}
SlurmdPort={{ $s.slurmdPort }}
AuthType={{ $s.authType }}
CryptoType={{ $s.cryptoType }}
AuthAltTypes={{ $s.authAltTypes }}
AuthAltParameters={{ $s.authAltParameters }}
{{ "" }}
{{- if $s.accounting.enabled }}
# Job accounting via slurmdbd (gracefully degrades if slurmdbd is unavailable)
AccountingStorageType={{ $s.accounting.storageType }}
AccountingStorageHost={{ $s.accounting.storageHost }}
AccountingStoragePort={{ $s.accounting.storagePort }}
JobAcctGatherType={{ $s.accounting.jobAcctGatherType }}
JobAcctGatherFrequency={{ $s.accounting.jobAcctGatherFrequency }}
{{- end }}
{{- $gresTypes := include "slurm-platform.gresTypes" . }}
{{- if $gresTypes }}
GresTypes={{ $gresTypes }}
{{- if and $s.accounting.enabled $s.accounting.storageTres }}
AccountingStorageTRES={{ join "," $s.accounting.storageTres }}
{{- end }}
{{- end }}
{{ "" }}
{{- range .Values.pools }}
{{- $pool := . }}
{{- $gresStr := "" }}
{{- $gresEntries := include "slurm-platform.gresList" $pool | fromJsonArray }}
{{- if $gresEntries }}
{{- $gresStr = printf " Gres=%s" (join "," $gresEntries) }}
{{- end }}
{{- $featureStr := "" }}
{{- if $pool.features }}
{{- $featureStr = printf " Feature=%s" (join "," $pool.features) }}
{{- end }}
{{- range $i, $_ := until (int $pool.maxNodes) -}}
{{- $nodeName := printf "%s-%d" $pool.statefulset $i }}
{{- $nodeAddr := printf "%s.%s.%s.svc.cluster.local" $nodeName $pool.statefulset $ns }}
NodeName={{ $nodeName }} NodeAddr={{ $nodeAddr }} NodeHostname={{ $nodeName }} CPUs={{ $pool.cpus }} RealMemory={{ $pool.realMemory }} Sockets={{ $pool.sockets }} CoresPerSocket={{ $pool.coresPerSocket }} ThreadsPerCore={{ $pool.threadsPerCore }} State=UNKNOWN{{ $featureStr }}{{ $gresStr }}
{{- end }}
{{- end }}
{{- range .Values.partitions }}
{{- $part := . }}
{{- $nodes := list -}}
{{- range $.Values.pools -}}
  {{- $pool := . -}}
  {{- if eq $pool.partition $part.name -}}
    {{- range $i, $_ := until (int $pool.maxNodes) -}}
      {{- $nodes = append $nodes (printf "%s-%d" $pool.statefulset $i) -}}
    {{- end -}}
  {{- end -}}
{{- end }}
{{- if $nodes }}
PartitionName={{ $part.name }} Nodes={{ join "," $nodes }} Default={{ if $part.default }}YES{{ else }}NO{{ end }} MaxTime={{ $part.maxTime }} State={{ default "UP" $part.state }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
-----------------------------------------------------------------------------
slurm-platform.gresConf — gres.conf body (one line per (node, gres entry)).

Mirrors render-core.py: mps entries get only `Name=mps Count=N` (no Type, no
File, since the device-plugin owns the MPS daemon and Slurm only tracks SM%).
GPU entries get `Name=gpu Type=<x> Count=<n> File=<dev>` where <dev> is
/dev/nvidia0 on k3s real-GPU runs and /dev/null in Kind dev mode.

Returns empty string when no pool has gres.
-----------------------------------------------------------------------------
*/}}
{{- define "slurm-platform.gresConf" -}}
{{- $devFile := "/dev/null" -}}
{{- if include "slurm-platform.realGpu" . -}}{{- $devFile = "/dev/nvidia0" -}}{{- end -}}
{{- $lines := list -}}
{{- range .Values.pools -}}
  {{- $pool := . -}}
  {{- $gresEntries := include "slurm-platform.gresList" $pool | fromJsonArray -}}
  {{- range $i, $_ := until (int $pool.maxNodes) -}}
    {{- $nodeName := printf "%s-%d" $pool.statefulset $i -}}
    {{- range $gresEntries -}}
      {{- $parts := splitList ":" . -}}
      {{- $name := index $parts 0 -}}
      {{- if eq $name "mps" -}}
        {{- $count := index $parts 1 -}}
        {{- $lines = append $lines (printf "NodeName=%s Name=mps Count=%s" $nodeName $count) -}}
      {{- else -}}
        {{- $type := index $parts 1 -}}
        {{- $count := index $parts 2 -}}
        {{- $lines = append $lines (printf "NodeName=%s Name=%s Type=%s Count=%s File=%s" $nodeName $name $type $count $devFile) -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- if $lines -}}
{{- join "\n" $lines -}}
{{- end -}}
{{- end -}}

{{/*
-----------------------------------------------------------------------------
slurm-platform.partitionsJson — JSON string consumed by the elastic operator
via the PARTITIONS_JSON env var. One object per pool, in pool order.

Schema (matches operator/main.py PoolConfig):
  partition, worker_statefulset, min_replicas, max_replicas,
  scale_up_step, scale_down_step, scale_down_cooldown,
  drain_timeout_seconds, match_features, match_gres, fallback
-----------------------------------------------------------------------------
*/}}
{{- define "slurm-platform.partitionsJson" -}}
{{- $items := list -}}
{{- range .Values.pools -}}
  {{- $obj := dict
      "partition" .partition
      "worker_statefulset" .statefulset
      "min_replicas" (int .minReplicas)
      "max_replicas" (int .maxReplicas)
      "scale_up_step" 1
      "scale_down_step" 1
      "scale_down_cooldown" (int (default 60 .scaleCooldownSeconds))
      "drain_timeout_seconds" (int (default 1800 $.Values.operator.drainTimeoutSeconds))
      "match_features" (default (list) .features)
      "match_gres" (default (list) .matchGres)
      "fallback" (default false .fallback) -}}
  {{- $items = append $items $obj -}}
{{- end -}}
{{- $items | toJson -}}
{{- end -}}
