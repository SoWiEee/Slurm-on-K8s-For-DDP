#!/usr/bin/env bash
# setup-linux-gpu.sh - Linux host prerequisites for GPU + MPS
#
# Run ONCE on the Linux host (as root) if the host is not already ready.
# Installs NVIDIA Container Toolkit, configures the containerd runtime,
# and can install k3s when requested.
#
# Usage:
#   sudo bash scripts/setup-linux-gpu.sh        # NVIDIA CT + configure containerd
#   sudo bash scripts/setup-linux-gpu.sh --k3s  # also install k3s when missing

set -euo pipefail

INSTALL_K3S=${INSTALL_K3S:-false}
for arg in "$@"; do
  case "$arg" in
    --k3s) INSTALL_K3S=true ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: sudo bash scripts/setup-linux-gpu.sh [--k3s]" >&2
      exit 1
      ;;
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

# k3s uses containerd; configure before k3s install so it picks up the runtime.
nvidia-ctk runtime configure --runtime=containerd --config=/etc/containerd/config.toml
if systemctl is-active --quiet k3s 2>/dev/null; then
  systemctl restart k3s
fi
echo "containerd configured for NVIDIA (k3s path)"

# ---------------------------------------------------------------------------
# Step 4 (optional): Install k3s with NVIDIA runtime
# ---------------------------------------------------------------------------
if [[ "$INSTALL_K3S" == "true" ]]; then
  echo ""
  echo "=== [4] Installing k3s ==="
  if command -v k3s >/dev/null 2>&1; then
    echo "k3s already installed: $(k3s --version | head -1)"
  else
    # Install k3s pointing at containerd and disable default traefik.
    # INSTALL_K3S_EXEC passes flags to k3s server.
    # Note: do NOT pass --kube-apiserver-arg feature-gates=... here;
    # kube-apiserver 1.28+ fatals on unknown gate names.
    INSTALL_K3S_EXEC="--container-runtime-endpoint unix:///run/containerd/containerd.sock \
      --disable traefik" \
      curl -sfL https://get.k3s.io | sh -

    # Allow non-root kubectl.
    # WARNING: running as root (sudo), so HOME=/root. The kubeconfig lands at
    # /root/.kube/config, not the invoking user's home. Copy it manually:
    #   sudo chmod 644 /etc/rancher/k3s/k3s.yaml
    #   mkdir -p ~/.kube && cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
    #   chmod 600 ~/.kube/config
    mkdir -p "${HOME}/.kube"
    cp /etc/rancher/k3s/k3s.yaml "${HOME}/.kube/config"
    chown "$(id -u):$(id -g)" "${HOME}/.kube/config"
    chmod 600 "${HOME}/.kube/config"

    echo "k3s installed. Kubeconfig: ${HOME}/.kube/config"
    echo "Context: default (use KUBECONFIG=~/.kube/config with deploy-1.sh)"
    echo "NOTE: if you ran this with sudo, copy kubeconfig to your user home:"
    echo "  mkdir -p ~/.kube && sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config"
    echo "  sudo chown \$(id -u):\$(id -g) ~/.kube/config && chmod 600 ~/.kube/config"
  fi

  # Wait for k3s to be ready.
  echo "Waiting for k3s node to be Ready..."
  until kubectl get nodes 2>/dev/null | grep -q " Ready"; do sleep 3; done
  kubectl get nodes
fi

echo ""
echo "=== Host GPU setup complete ==="
echo ""
echo "Next steps:"
echo "  export KUBECONFIG=~/.kube/config"
echo "  bash scripts/deploy-1.sh"
echo "  helm install slurm-platform ./chart -f chart/values-k3s.yaml -n slurm --create-namespace"
echo ""
echo "Optional GPU node labels for per-model MPS/exclusive policies:"
echo "  kubectl label node <RTX4070-NODE> nvidia.com/device-plugin.config=rtx4070-mps"
echo "  kubectl label node <RTX4080-NODE> nvidia.com/device-plugin.config=rtx4080-exclusive"
