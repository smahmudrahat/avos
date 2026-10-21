#!/usr/bin/env bash
# Build the base kernel + rootfs once: Firecracker CI Ubuntu 24.04 rootfs with the agent baked in,
# plus harness.ext4: a read-only disk shared by every VM, mounted at /opt/harness
# (τ²-bench, mini-swe-agent, each in its own venv, and the coding tasks from eval/coding).
# Output: /var/lib/avos/images/{vmlinux,rootfs.ext4,harness.ext4,harness.versions.json}. Run on the host with sudo.
set -euo pipefail
IMG=/var/lib/avos/images
CI=https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.15/x86_64
TAU2_COMMIT=96a3ef92329baf908ffe9dfe188cea34e8701d2a # docs/versions.md
HERE=$(cd "$(dirname "$0")/.." && pwd)

mkdir -p "$IMG" && cd "$IMG"
[ -f vmlinux ] || curl -fsSL -o vmlinux "$CI/vmlinux-6.1.155"
[ -f ubuntu-24.04.squashfs ] || curl -fsSL -o ubuntu-24.04.squashfs "$CI/ubuntu-24.04.squashfs"

rm -rf rootfs && unsquashfs -q -d rootfs ubuntu-24.04.squashfs >/dev/null
install -D -m 0755 "$HERE/agent/agent.py" rootfs/opt/avos/agent.py
install -D -m 0755 "$HERE/agent/demo_agent.py" rootfs/opt/avos/demo_agent.py
install -D -m 0644 "$HERE/agent/avos-agent.service" rootfs/etc/systemd/system/avos-agent.service
# SPIRE agent binary, same release as the host's server (identity/install.sh puts it in /opt/spire first).
install -D -m 0755 /opt/spire/bin/spire-agent rootfs/opt/spire/bin/spire-agent
# Host-only root key for the POC 3 hand tests; guests are reachable only from the host.
[ -f vm_key ] || ssh-keygen -q -t ed25519 -N "" -C avos-host -f vm_key
install -D -m 0600 vm_key.pub rootfs/root/.ssh/authorized_keys
ln -sf /etc/systemd/system/avos-agent.service rootfs/etc/systemd/system/multi-user.target.wants/avos-agent.service
# The kernel `ip=` boot arg configures eth0 (with a gateway); fcnet would only add a duplicate address.
rm -f rootfs/etc/systemd/system/*/fcnet.service
echo "nameserver 1.1.1.1" > rootfs/etc/resolv.conf # for the chroot installs below; emptied before packing
mkdir -p rootfs/opt/harness
echo "/dev/vdb /opt/harness ext4 ro,nofail 0 0" >> rootfs/etc/fstab

# Harness disk. The venv is built inside the guest rootfs (chroot) so its paths and Python match the VM.
rm -rf harness && mkdir -p harness
git clone -q https://github.com/sierra-research/tau2-bench harness/tau2-bench
git -C harness/tau2-bench checkout -q "$TAU2_COMMIT"
rm -rf harness/tau2-bench/.git harness/tau2-bench/data/tau2/results # 1 GB we never read
# τ² hard-codes gpt-4.1 as its NL-assertion judge; read it from TAU2_NL_JUDGE instead (docs/conventions.md §2: open-weight judge).
cfg=harness/tau2-bench/src/tau2/config.py
grep -q '^DEFAULT_LLM_NL_ASSERTIONS = "gpt-4.1-2025-04-14"$' $cfg || { echo "τ² judge line changed; update the patch" >&2; exit 1; }
sed -i 's/^DEFAULT_LLM_NL_ASSERTIONS = .*/import os\nDEFAULT_LLM_NL_ASSERTIONS = os.environ.get("TAU2_NL_JUDGE", "gpt-4.1-2025-04-14")/' $cfg
curl -LsSf https://astral.sh/uv/0.13.1/install.sh | env UV_INSTALL_DIR="$IMG/harness/bin" UV_NO_MODIFY_PATH=1 sh -s -- -q
mount --bind harness rootfs/opt/harness
mount --bind /dev rootfs/dev
mount -t proc proc rootfs/proc
trap 'umount rootfs/proc rootfs/dev rootfs/opt/harness 2>/dev/null || true' EXIT
uvc() { chroot rootfs env UV_PYTHON=/usr/bin/python3.12 UV_PYTHON_DOWNLOADS=never UV_CACHE_DIR=/tmp/uv \
  UV_LINK_MODE=copy /opt/harness/bin/uv "$@"; }
uvc sync -q --frozen --compile-bytecode --project /opt/harness/tau2-bench
# τ² at this commit imports its voice module from core and needs websockets (docs/versions.md).
uvc pip install -q --compile-bytecode --python /opt/harness/tau2-bench/.venv/bin/python websockets==17.2
# mini-swe-agent, with LiteLLM pinned to τ²'s version (newer LiteLLM fails to import without `mcp`).
uvc venv -q /opt/harness/mini-swe-agent/.venv
uvc pip install -q --compile-bytecode --python /opt/harness/mini-swe-agent/.venv/bin/python \
  mini-swe-agent==2.4.6 litellm==1.81.11
uvc pip freeze --python /opt/harness/mini-swe-agent/.venv/bin/python > harness/mini-swe-agent/freeze.txt
cp -r "$HERE/../eval/coding" harness/tasks
umount rootfs/proc rootfs/dev rootfs/opt/harness && trap - EXIT
rm -rf rootfs/tmp/uv
# No DNS in guests: they have no route out (the gateway resolves names on the host). An empty resolv.conf makes
# lookups fail at once instead of timing out against an unreachable server.
: > rootfs/etc/resolv.conf
rm -f harness.commit
echo "{\"tau2\": \"$TAU2_COMMIT\", \"mini-swe-agent\": \"$(grep -oP '^mini-swe-agent==\K.*' harness/mini-swe-agent/freeze.txt)\"}" > harness.versions.json

rm -f harness.ext4 && truncate -s $(($(du -sm harness | cut -f1) * 12 / 10 + 64))M harness.ext4
mkfs.ext4 -q -d harness -F harness.ext4
rm -f rootfs.ext4 && truncate -s 1G rootfs.ext4 && mkfs.ext4 -q -d rootfs -F rootfs.ext4
rm -rf rootfs harness
ls -la "$IMG"
