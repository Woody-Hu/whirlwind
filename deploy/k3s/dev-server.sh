#!/usr/bin/env bash
# Launch k3s on a dev sandbox / container (ADR-0005 D4).
#
# Full agent mode (kubelet + containerd) needs CAP_SYS_ADMIN, a writable
# cgroup mount and writable /proc/sys — none of which a default container has.
# This script therefore runs k3s in `--disable-agent` mode: a real Kubernetes
# control plane (kube-apiserver + controller-manager + scheduler over sqlite),
# which validates manifests, controllers, scheduling and PVC binding for real.
# Pods get scheduled but stop at ContainerCreating until a real node joins:
#
#   ./dev-server.sh              # start control plane, wait for /readyz
#   ./dev-server.sh --fake-node  # + register a Ready fake node + static PV so
#                                 # pods schedule and PVC binds (dev only!)
#
# Environment:
#   K3S_DATA_DIR (default /var/lib/rancher/k3s)
#   KUBECONFIG   (default /etc/rancher/k3s/kube.yaml)

set -euo pipefail

DATA_DIR="${K3S_DATA_DIR:-/var/lib/rancher/k3s}"
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/kube.yaml}"
FAKE_NODE=0
[[ "${1:-}" == "--fake-node" ]] && FAKE_NODE=1

mkdir -p "$DATA_DIR" "$(dirname "$KUBECONFIG")"

# kubelet wants /dev/kmsg even without an agent (startup probe); containers
# often lack it
if [[ ! -c /dev/kmsg ]]; then
  mknod /dev/kmsg c 1 11 2>/dev/null || echo "warning: cannot mknod /dev/kmsg" >&2
fi

if pgrep -f "k3s server" >/dev/null 2>&1; then
  echo "k3s server already running"
else
  echo "starting k3s server (--disable-agent)..."
  nohup k3s server \
    --disable-agent \
    --disable=traefik --disable=servicelb --disable=metrics-server \
    --disable-network-policy --disable=local-storage --disable-cloud-controller \
    --egress-selector-mode=disabled \
    --flannel-backend=none \
    --write-kubeconfig="$KUBECONFIG" --write-kubeconfig-mode=644 \
    --data-dir="$DATA_DIR" \
    --https-listen-port=6443 \
    --bind-address=127.0.0.1 --advertise-address=127.0.0.1 \
    --cluster-cidr=10.42.0.0/16 --service-cidr=10.43.0.0/16 \
    > /var/log/k3s.log 2>&1 &
  echo "k3s pid: $!"
fi

# wait for the API to be ready
for _ in $(seq 1 60); do
  if kubectl get --raw='/readyz' >/dev/null 2>&1; then
    echo "control plane ready ($(kubectl version -o json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"])' 2>/dev/null || echo k3s))"
    break
  fi
  sleep 2
done
kubectl get --raw='/readyz' >/dev/null 2>&1 || { echo "control plane never became ready; see /var/log/k3s.log" >&2; exit 1; }

if [[ "$FAKE_NODE" -eq 1 ]]; then
  echo "registering fake node + static PV (dev only)..."
  kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Node
metadata:
  name: dev-node
  labels:
    kubernetes.io/hostname: dev-node
    kubernetes.io/arch: amd64
    kubernetes.io/os: linux
spec: {}
EOF
  # mark Ready with capacity/allocatable (status is a subresource)
  kubectl patch node dev-node --subresource=status --type=merge -p '{
    "status": {
      "capacity": {"cpu": "8", "memory": "16Gi", "pods": "110"},
      "allocatable": {"cpu": "8", "memory": "16Gi", "pods": "110"},
      "conditions": [{"type": "Ready", "status": "True", "reason": "FakeNode", "message": "dev fake node (no kubelet)"}],
      "nodeInfo": {"kubeletVersion": "v1.36.3", "containerRuntimeVersion": "fake://dev", "osImage": "dev", "operatingSystem": "linux", "architecture": "amd64"}
    }
  }'

  # Without kubelet heartbeats the node-lifecycle-controller marks the node
  # NotReady within ~40s and taints it unreachable, which blocks scheduling.
  # Keep it Ready the way KWOK does: renew the NodeLease + refresh the Ready
  # condition every 10s (daemonized; idempotent across reruns).
  if ! pgrep -f "fake-node-keepalive" >/dev/null 2>&1; then
    cat > /tmp/fake-node-keepalive.sh <<'KEEPALIVE'
#!/usr/bin/env bash
set -uo pipefail
while true; do
  now="$(date -u +%Y-%m-%dT%H:%M:%S.000000Z)"
  if ! kubectl -n kube-node-lease get lease dev-node >/dev/null 2>&1; then
    kubectl apply -f - <<EOF
apiVersion: coordination.k8s.io/v1
kind: Lease
metadata:
  name: dev-node
  namespace: kube-node-lease
spec:
  holderIdentity: dev-node
  leaseDurationSeconds: 40
  renewTime: "$now"
EOF
  else
    kubectl -n kube-node-lease patch lease dev-node --type=merge -p "{\"spec\":{\"renewTime\":\"$now\"}}" >/dev/null 2>&1
  fi
  kubectl patch node dev-node --subresource=status --type=merge -p \
    "{\"status\":{\"conditions\":[{\"type\":\"Ready\",\"status\":\"True\",\"reason\":\"FakeNode\",\"lastHeartbeatTime\":\"$now\"}]}}" >/dev/null 2>&1
  sleep 10
done
KEEPALIVE
    chmod +x /tmp/fake-node-keepalive.sh
    nohup /tmp/fake-node-keepalive.sh > /var/log/fake-node-keepalive.log 2>&1 &
    echo "fake-node keepalive started (pid $!)"
  fi

  # local-path storage class + pre-bound PV so the whirlwind PVC binds
  # (the real k3s local-path provisioner is disabled in this mode)
  mkdir -p /tmp/whirlwind-pv
  kubectl apply -f - <<'EOF'
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: local-path
provisioner: rancher.io/local-path
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
EOF
  cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: PersistentVolume
metadata:
  name: whirlwind-data-pv
spec:
  capacity:
    storage: 2Gi
  accessModes: ["ReadWriteOnce"]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: local-path
  claimRef:
    apiVersion: v1
    kind: PersistentVolumeClaim
    name: whirlwind-data
    namespace: whirlwind
  local:
    path: /tmp/whirlwind-pv
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values: ["dev-node"]
EOF
  echo "fake node ready — pods will schedule onto dev-node and stop at ContainerCreating (no kubelet)"
fi
