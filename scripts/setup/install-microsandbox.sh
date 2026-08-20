#!/usr/bin/env bash
# install-microsandbox.sh — install the microsandbox (msb) CLI + libkrunfw kernel.
#
# OS REQUIREMENTS (strict):
#   - Linux x86_64 / aarch64, or macOS aarch64 (Apple Silicon). Windows is
#     out of scope for this script (use the official install.ps1).
#   - network egress to github.com release downloads (honors HTTP(S)_PROXY;
#     an optional GITHUB_TOKEN is attached when set — unauthenticated API
#     lookups rate-limit to 403 in some egress environments, but pinned
#     release-asset downloads do not need it).
#   - MSB_HOME (default ~/.microsandbox) must be writable; msb is symlinked
#     into ~/.local/bin when writable (else a PATH reminder is printed).
#
# RUNTIME requirements this script CANNOT install (documented for honesty):
#   - Linux: /dev/kvm must be backed by a loaded host kvm module. A missing
#     node is NOT fixable with mknod — verified 2026-08-20 on a kernel
#     without the kvm module: the node exists yet open(2) fails ENODEV, and
#     `msb doctor` reports "KVM access unavailable" truthfully. Containers
#     additionally need the device passed through; there is no TCG/QEMU
#     fallback in microsandbox (libkrun is KVM/HVF/WHP only).
#   - macOS: Apple Silicon (HVF via Virtualization.framework); Intel macs
#     are unsupported by libkrun.
#   - glibc >= 2.28 on Linux (manylinux_2_28 baseline, per upstream).
#
# What gets installed (mirrors the official installer layout):
#   $MSB_HOME/bin/msb                     — the CLI
#   $MSB_HOME/lib/libkrunfw.so.<ver>      — embedded guest kernel (Linux)
#   $MSB_HOME/lib/libkrunfw.<abi>.dylib   — same, macOS
#
# The bundle tarball ships the correctly versioned library name that `msb`
# resolves at startup (msb 0.6.12 pairs with libkrunfw.so.5.6.1), which is
# why this script installs from the bundle instead of the standalone
# platform-suffixed .so asset.
#
# Verification afterwards:
#   msb --version && msb doctor
#   uv run python scripts/run_tests.py tests/integration/test_microsandbox_driver.py -q
#
# macOS note: the darwin-aarch64 branch follows the same layout but is
# structurally-untested from a Linux container (2026-08-20); verify with
# `msb doctor` on a real Apple-Silicon host.
set -euo pipefail

MSB_VERSION="${MSB_VERSION:-v0.6.12}"
MSB_HOME="${MSB_HOME:-$HOME/.microsandbox}"
SEGMENTS="${SEGMENTS:-8}"

OS="$(uname -s)"
ARCH="$(uname -m)"
case "${OS}:${ARCH}" in
  Linux:x86_64)   BUNDLE="microsandbox-linux-x86_64.tar.gz" ;;
  Linux:aarch64)  BUNDLE="microsandbox-linux-aarch64.tar.gz" ;;
  Darwin:arm64)   BUNDLE="microsandbox-darwin-aarch64.tar.gz" ;;
  *)
    echo "error: unsupported ${OS}/${ARCH} (Linux x86_64|aarch64, macOS arm64)" >&2
    exit 1
    ;;
esac

BASE_URL="https://github.com/superradcompany/microsandbox/releases/download/${MSB_VERSION}"

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

curl_auth() { # attach the optional token when provided
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    curl -fsSL -H "Authorization: Bearer ${GITHUB_TOKEN}" "$@"
  else
    curl -fsSL "$@"
  fi
}

fetch() { # fetch <url> <dest> [segments] — ranged parallel GET for big files
  local url="$1" dest="$2" segments="${3:-${SEGMENTS}}"
  local size
  size="$(curl -fsSI "${url}" | tr -d '\r' | awk 'tolower($1)=="content-length:"{print $2}' | tail -1)"
  if [ -z "${size}" ] || [ "${segments}" -le 1 ] || [ "${size}" -lt 8388608 ]; then
    curl_auth "${url}" -o "${dest}"
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
  for i in $(seq 0 $((segments - 1))); do cat "${dest}.part${i}"; done > "${dest}"
  rm -f "${dest}".part*
  local got
  got="$(stat -c%s "${dest}" 2>/dev/null || stat -f%z "${dest}")"
  [ "${got}" = "${size}" ] || { echo "error: assembled ${got} != ${size} bytes" >&2; return 1; }
}

echo ">> downloading microsandbox ${MSB_VERSION} (${OS}/${ARCH}) ..."
fetch "${BASE_URL}/${BUNDLE}" "${TMP}/${BUNDLE}"

echo ">> verifying sha256 ..."
curl_auth "${BASE_URL}/checksums.sha256" -o "${TMP}/checksums.sha256"
( cd "${TMP}" && grep "  ${BUNDLE}\$" checksums.sha256 | sha256sum -c 2>/dev/null \
    || grep "  ${BUNDLE}\$" checksums.sha256 | shasum -a 256 -c )

echo ">> extracting ..."
tar -xzf "${TMP}/${BUNDLE}" -C "${TMP}"

mkdir -p "${MSB_HOME}/bin" "${MSB_HOME}/lib"
install -m 755 "${TMP}/msb" "${MSB_HOME}/bin/msb"

if [ "${OS}" = "Linux" ]; then
  set -- "${TMP}"/libkrunfw.so.*.*.*
  if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
    echo "error: release bundle must contain exactly one versioned libkrunfw.so" >&2; exit 1
  fi
  install -m 644 "$1" "${MSB_HOME}/lib/$(basename "$1")"
  _abi="$(basename "$1" | sed 's/^libkrunfw\.so\.//; s/\..*//')"
  ln -sf "$(basename "$1")" "${MSB_HOME}/lib/libkrunfw.so.${_abi}"
else
  set -- "${TMP}"/libkrunfw.*.dylib
  if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
    echo "error: release bundle must contain exactly one versioned libkrunfw dylib" >&2; exit 1
  fi
  install -m 644 "$1" "${MSB_HOME}/lib/$(basename "$1")"
fi

# PATH convenience (the binary itself stays under MSB_HOME beside its lib)
mkdir -p "${HOME}/.local/bin"
if ln -sf "${MSB_HOME}/bin/msb" "${HOME}/.local/bin/msb" 2>/dev/null; then
  echo "installed: ${MSB_HOME}/bin/msb (+ ~/.local/bin/msb symlink)"
else
  echo "installed: ${MSB_HOME}/bin/msb (add it to PATH)"
fi

echo ">> $("${MSB_HOME}/bin/msb" --version 2>/dev/null || echo 'msb')"
echo ">> runtime readiness (honest — install is NOT runtime):"
"${MSB_HOME}/bin/msb" doctor 2>&1 | sed 's/^/   /' || true
echo "done. On Linux, a working sandbox needs a real KVM: a /dev/kvm node"
echo "without a loaded host kvm module fails ENODEV (not installable here)."
