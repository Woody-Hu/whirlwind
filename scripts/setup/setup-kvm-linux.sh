#!/usr/bin/env bash
# setup-kvm-linux.sh — prepare working KVM on a Linux host so microsandbox's
# real VM tests can run locally (the dev sandbox has no KVM: no CAP_SYS_ADMIN
# and the host kvm module not loaded → /dev/kvm open() returns ENODEV).
#
# AUTO-REFUSES before it touches anything (honesty-first, no fake success):
#   - non-Linux host            → exit (macOS uses HVF, see install-microsandbox.sh)
#   - kernel lacks vmx/svm      → exit (CPU virtualization not exposed at all)
#   - kvm module already loaded → the job is done; print status and prove open()
#   - not root (or CAP_SYS_ADMIN) → exit; KVM is a kernel feature, it cannot be
#     made available in an unprivileged container — that is a platform gate,
#     not an installable package.
#
# OS REQUIREMENTS (STRICT — do not run inside an unprivileged container):
#   - Linux, x86_64 with VMX or aarch64 with a virtualization-capable CPU.
#   - root / CAP_SYS_ADMIN in the current user namespace.
#   - Host kernel CONFIG_KVM / CONFIG_KVM_INTEL / CONFIG_KVM_AMD present
#     (kmods `kvm`, `kvm_intel`, `kvm_amd` loadable).
#   - When running inside a VM: the L0 hypervisor must expose nested
#     virtualization (e.g. `modprobe kvm_intel nested=1` on the L1 guest, and
#     nested TOGGLE on the L0 side). Inside a container you additionally need
#     the host to pass /dev/kvm through.
#
# What it does:
#   - modprobe kvm, kvm_intel/kvm_amd (with nested=1 on the module params)
#   - create /dev/kvm if missing (mknod + chmod 666; only a real fix when the
#     module actually loads — a bare mknod without the module still ENODEVs,
#     which this script detects and reports honestly)
#   - final probe: open(2) /dev/kvm and run `msb doctor` (if msb installed).
#
# Verification afterwards:
#   msb --version && msb doctor
#   uv run python scripts/run_tests.py tests/integration/test_microsandbox_driver.py -q
set -euo pipefail

NESTED="${NESTED:-1}" # enable nested virtualization when loading kvm_intel/kvm_amd

log() { printf '[kvm] %s\n' "$*"; }
die() { printf '[kvm] ERROR: %s\n' "$*" >&2; exit 1; }

# ---- 1. platform gate ----
OS="$(uname -s)"
case "${OS}" in
  Linux) ;;
  *) die "KVM is Linux-only (${OS}); on macOS use install-microsandbox.sh's HVF path (Apple Silicon)." ;;
esac

log "host: $(uname -sr) / $(uname -m)"

# ---- 2. CPU/nested gate ----
if ! grep -qE '\b(vmx|svm)\b' /proc/cpuinfo; then
  die "CPU virtualization flag (vmx/svm) not present in /proc/cpuinfo — the host (or L0, if this is a VM) does not expose nested virtualization; nothing to load."
fi
log "CPU virtualization flag present."

# ---- 3. already-done fast path ----
if python3 -c 'import os; os.open("/dev/kvm", os.O_RDWR); os.close(0)' 2>/dev/null; then
  log "/dev/kvm ALREADY openable — KVM is available; no changes made."
  exit 0
fi
log "/dev/kvm not currently openable; attempting to enable KVM."

# ---- 4. privilege gate ----
if [ "$(id -u)" -ne 0 ]; then
  die "need root (or CAP_SYS_ADMIN) to load kernel modules. This is a platform/kernel feature — it CANNOT be enabled from an unprivileged container. Run this on a real host (or a privileged nested-virt VM) as root."
fi

# ---- 5. load modules ----
log "loading kvm / kvm_intel (nested=${NESTED}) / kvm_amd ..."
modprobe kvm 2>/dev/null || true
if grep -q ' vmx ' /proc/cpuinfo; then
  modprobe kvm_intel nested="${NESTED}" 2>/dev/null || modprobe kvm_intel 2>/dev/null || log "modprobe kvm_intel failed; kvm_amd path will try next"
else
  modprobe kvm_amd nested="${NESTED}" 2>/dev/null || modprobe kvm_amd 2>/dev/null || die "modprobe kvm_amd failed — kernel kvm module unavailable on this host."
fi

# ---- 6. device node ----
if [ ! -e /dev/kvm ]; then
  log "creating /dev/kvm (10:232)"
  mknod /dev/kvm c 10 232 2>/dev/null || true
  chmod 666 /dev/kvm 2>/dev/null || true
fi

# ---- 7. honest final probe ----
if python3 -c 'import os; os.open("/dev/kvm", os.O_RDWR); os.close(0); print("[kvm] /dev/kvm opens OK")' 2>/dev/null; then
  log "KVM is now available."
  if command -v msb >/dev/null; then
    msb doctor || log "msb doctor reported an issue — see output above (may be missing libkrunfw, fixable via scripts/setup/install-microsandbox.sh)."
  else
    log "msb not installed — run scripts/setup/install-microsandbox.sh next, then re-verify with msb doctor."
  fi
  exit 0
fi
die "still cannot open /dev/kvm after loading modules (open() ENODEV). This means the kvm module did not actually load — the host kernel may lack CONFIG_KVM, or inside a VM the L0 hypervisor does not honor nested virtualization. This is a host/platform capability, not something a script can install."