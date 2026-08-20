#!/usr/bin/env bash
# install-runsc.sh — install the gVisor runsc binary + static busybox.
#
# OS REQUIREMENTS (strict):
#   - Linux only (gVisor is a Linux userspace kernel; macOS hosts must run
#     this inside a Linux VM, e.g. colima's docker VM).
#   - arch: x86_64 or aarch64.
#   - root or writable /usr/local/bin (falls back to ~/.local/bin on EACCES,
#     printing a PATH reminder).
#   - network egress to storage.googleapis.com and busybox.net (honors
#     HTTP(S)_PROXY).
#
# Download strategy: single-stream curl was measured at ~30 KB/s through a
# corporate proxy while 8 parallel ranged requests saturated ~300 KB/s
# (2026-08-20 baseline) — so large fetches use SEGMENTS parallel range GETs
# (set SEGMENTS=1 to force single-stream), then reassemble and verify the
# upstream sha512 before install.
#
# NOT installed by this script (out of scope, documented for honesty):
#   - the Docker/containerd runtime wiring (`runsc install`) — the whirlwind
#     RunscDriver invokes the runsc binary directly;
#   - /dev/vsock — a kernel/device property, not installable.
#
# Verification afterwards:
#   runsc --version && busybox | head -1
#   uv run python scripts/run_tests.py tests/integration/test_runsc_driver.py -q
set -euo pipefail

ARCH="$(uname -m)"
case "${ARCH}" in
  x86_64|aarch64) ;;
  *) echo "error: unsupported arch ${ARCH} (need x86_64 or aarch64)" >&2; exit 1 ;;
esac
if [ "$(uname -s)" != "Linux" ]; then
  echo "error: runsc is Linux-only; on macOS run this inside a Linux VM (colima)" >&2
  exit 1
fi

BASE_URL="https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}"
BUSYBOX_VER="1.35.0"   # latest published on busybox.net/downloads/binaries
BUSYBOX_URL="https://www.busybox.net/downloads/binaries/${BUSYBOX_VER}-${ARCH}-linux-musl/busybox"
SEGMENTS="${SEGMENTS:-8}"

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

fetch() { # fetch <url> <dest> [segments]
  local url="$1" dest="$2" segments="${3:-${SEGMENTS}}"
  local size
  size="$(curl -fsSI "${url}" | tr -d '\r' | awk 'tolower($1)=="content-length:"{print $2}' | tail -1)"
  if [ -z "${size}" ] || [ "${segments}" -le 1 ] || [ "${size}" -lt 8388608 ]; then
    curl -fsSL "${url}" -o "${dest}"
    return
  fi
  echo ">> segmented fetch: ${segments} x $((size / segments / 1024))KiB avg (total $((size / 1024 / 1024))MiB)"
  local chunk=$((size / segments)) i start end
  local pids=()
  for i in $(seq 0 $((segments - 1))); do
    start=$((i * chunk))
    if [ "${i}" -eq $((segments - 1)) ]; then end=$((size - 1)); else end=$(( (i + 1) * chunk - 1 )); fi
    curl -fsS -r "${start}-${end}" "${url}" -o "${dest}.part${i}" &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do wait "${pid}"; done
  cat "$(ls "${dest}".part* | sort -V)" > "${dest}"
  rm -f "${dest}".part*
  local got
  got="$(stat -c%s "${dest}")"
  [ "${got}" = "${size}" ] || { echo "error: assembled ${got} != ${size} bytes" >&2; return 1; }
}

echo ">> downloading runsc (${ARCH}) ..."
fetch "${BASE_URL}/runsc" "${TMP}/runsc"
curl -fsSL "${BASE_URL}/runsc.sha512" -o "${TMP}/runsc.sha512"
( cd "${TMP}" && grep -o '[0-9a-f]\{128\}' runsc.sha512 | head -1 \
    | awk '{print $1"  runsc"}' > runsc.sha512.sum && sha512sum -c runsc.sha512.sum )

echo ">> downloading static busybox (${ARCH}) ..."
fetch "${BUSYBOX_URL}" "${TMP}/busybox"
chmod a+rx "${TMP}/runsc" "${TMP}/busybox"

install_bin() {
  local src="$1" dst_dir="/usr/local/bin"
  if [ -w "${dst_dir}" ] || [ "$(id -u)" = "0" ]; then
    mv "${src}" "${dst_dir}/$(basename "${src}")"
    echo "installed: ${dst_dir}/$(basename "${src}")"
  else
    mkdir -p "${HOME}/.local/bin"
    mv "${src}" "${HOME}/.local/bin/$(basename "${src}")"
    echo "installed: ${HOME}/.local/bin/$(basename "${src}") (ensure it is on PATH)"
  fi
}

install_bin "${TMP}/runsc"
install_bin "${TMP}/busybox"

echo ">> runsc $(runsc --version 2>/dev/null | head -1 || echo 'on PATH?')"
echo "done. Restricted containers (no CAP_SYS_ADMIN) automatically get the"
echo "rootless + --network=none fallback in the driver; rootless runsc does"
echo "not support restore (upstream limitation, see MEMORY)."
