#!/usr/bin/env bash
# Build the whirlwind OCI image and import it into the k3s containerd (ADR-0005 D4).
#
# Usage:
#   ./build-image.sh [image-tag]        # default tag: whirlwind:dev
#
# Requires docker OR podman OR nerdctl to build, and a running k3s with its
# containerd socket (/run/k3s/containerd/containerd.sock) to import into.
# On hosts where the build tool or k3s containerd is unavailable the script
# still leaves a `whirlwind-<tag>.tar` on disk (docker save format) so the
# image can be imported elsewhere (e.g. `k3s ctr images import`).

set -euo pipefail

TAG="${1:-whirlwind:dev}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
TAR="${TAG%%:*}.tar"

for tool in docker podman nerdctl; do
  if command -v "$tool" >/dev/null 2>&1; then
    CLI="$tool"
    break
  fi
done

if [[ -z "${CLI:-}" ]]; then
  echo "no container builder found (docker/podman/nerdctl); cannot build" >&2
  exit 1
fi

echo "building $TAG with $CLI..."
"$CLI" build -t "$TAG" -f "$HERE/Dockerfile" "$REPO_ROOT"

if [[ -S /run/k3s/containerd/containerd.sock ]]; then
  echo "importing into k3s containerd..."
  "$CLI" save "$TAG" -o "$TAR"
  k3s ctr images import "$TAR"
  rm -f "$TAR"
  echo "done: $TAG available to k3s (use imagePullPolicy: IfNotPresent)"
else
  echo "k3s containerd socket not found; image left as $TAR (import with: k3s ctr images import $TAR)"
  "$CLI" save "$TAG" -o "$TAR"
fi
