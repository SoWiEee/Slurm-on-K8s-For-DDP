#!/usr/bin/env bash
# setup-linux-gpu.sh — Linux host prerequisites for GPU + MPS
#
# Run ONCE on the Linux host (as root) before bootstrap.sh.
# Installs: NVIDIA Container Toolkit, configures containerd runtime,
# optionally installs k3s.
#
# Usage:
#   sudo bash scripts/setup-linux-gpu.sh             # NVIDIA CT + configure containerd
#   sudo bash scripts/setup-linux-gpu.sh --k3s       # also install k3s
#   sudo bash scripts/setup-linux-gpu.sh --k3s --kind # install k3s AND configure Kind GPU

set -euo pipefail

INSTALL_K3S=${INSTALL_K3S:-false}
INSTALL_KIND_GPU=${INSTALL_KIND_GPU:-false}
for arg in "$@"; do
  case "$arg" in
    --k3s) INSTALL_K3S=true ;;
    --kind) INSTALL_KIND_GPU=true ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo bash scripts/setup-linux-gpu.sh" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Step 1: Verify NVIDIA driver is installed
# ---------------------------------------------------------------------------
echo "=== [1] Checking NVIDIA driver ==="
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "ERROR: nvidia-smi not found. Install NVIDIA driver first:" >&2
  echo "  Ubuntu 22.04: sudo apt install nvidia-driver-535" >&2
  echo "  Or use: ubuntu-drivers autoinstall" >&2
  exit 1
fi
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
echo "NVIDIA driver OK"

# ---------------------------------------------------------------------------
# Step 2: Install NVIDIA Container Toolkit
# ---------------------------------------------------------------------------
echo ""
echo "=== [2] Installing NVIDIA Container Toolkit ==="
if command -v nvidia-ctk >/dev/null 2>&1; then
  echo "nvidia-ctk already installed: $(nvidia-ctk --version 2>&1 | head -1)"
else
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
    gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

  apt-get update
  apt-get install -y nvidia-container-toolkit
  echo "nvidia-ctk installed: $(nvidia-ctk --version 2>&1 | head -1)"
fi

# ---------------------------------------------------------------------------
# Step 3: Configure container runtime for NVIDIA
# ---------------------------------------------------------------------------
echo ""
echo "=== [3] Configuring container runtime ==="

if [[ "$INSTALL_K3S" == "true" ]]; then
  # k3s uses containerd; configure before k3s install so it picks up the runtime.
  nvidia-ctk runtime configure --runtime=containerd --config=/etc/containerd/config.toml
  echo "containerd configured for NVIDIA (k3s path)"
else
  # Docker (used by Kind).
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
  echo "Docker configured for NVIDIA"
fi

# ---------------------------------------------------------------------------
# Step 4 (optional): Install k3s with NVIDIA runtime
# ---------------------------------------------------------------------------
if [[ "$INSTALL_K3S" == "true" ]]; then
  echo ""
  echo "=== [4] Installing k3s ==="
  if command -v k3s >/dev/null 2>&1; then
    echo "k3s already installed: $(k3s --version | head -1)"
  else
    # Install k3s with nvidia runtime and disable default traefik.
    # INSTALL_K3S_EXEC passes flags to k3s server.
    INSTALL_K3S_EXEC="--container-runtime-endpoint unix:///run/containerd/containerd.sock \
      --disable traefik \
      --kube-apiserver-arg feature-gates=GangScheduling=true,GenericWorkload=true" \
      curl -sfL https://get.k3s.io | sh -

    # Allow non-root kubectl (copy kubeconfig to ~/.kube/config).
    mkdir -p "${HOME}/.kube"
    cp /etc/rancher/k3s/k3s.yaml "${HOME}/.kube/config"
    chown "$(id -u):$(id -g)" "${HOME}/.kube/config"
    chmod 600 "${HOME}/.kube/config"

    echo "k3s installed. Kubeconfig: ${HOME}/.kube/config"
    echo "Context: default (use KUBE_CONTEXT=default with bootstrap.sh)"
  fi

  # Wait for k3s to be ready.
  echo "Waiting for k3s node to be Ready..."
  until kubectl get nodes 2>/dev/null | grep -q " Ready"; do sleep 3; done
  kubectl get nodes
fi

# ---------------------------------------------------------------------------
# Step 5 (optional): Kind GPU config hint
# ---------------------------------------------------------------------------
if [[ "$INSTALL_KIND_GPU" == "true" ]]; then
  echo ""
  echo "=== [5] Kind GPU config ==="
  echo "Use kind-config-gpu.yaml when creating the Kind cluster:"
  echo "  KIND_CONFIG=kind-config-gpu.yaml bash scripts/bootstrap.sh"
  echo ""
  echo "The config mounts /dev/nvidia* and sets runtimeClassName to nvidia."
fi

echo ""
echo "=== Host GPU setup complete ==="
echo ""
echo "Next steps:"
if [[ "$INSTALL_K3S" == "true" ]]; then
  echo "  K8S_RUNTIME=k3s REAL_GPU=true bash scripts/bootstrap.sh"
  echo "  bash scripts/bootstrap-gpu.sh   # device-plugin (sharing.mps built-in)"
  echo ""
  echo "  # Label each GPU node so device-plugin picks the right sharing config:"
  echo "  kubectl label node <RTX5070-NODE> nvidia.com/device-plugin.config=rtx5070-mps"
  echo "  kubectl label node <RTX4080-NODE> nvidia.com/device-plugin.config=rtx4080-exclusive"
else
  echo "  KIND_CONFIG=kind-config-gpu.yaml bash scripts/bootstrap.sh"
  echo "  bash scripts/bootstrap-gpu.sh"
fi
