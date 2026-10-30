#!/usr/bin/env bash
# Bakes the hermes CI runner image on Ubuntu 24.04. Mirrors hosts/hermes-hub/gh-runner/Dockerfile
# (sudo, build-essential, pkg-config, libssl-dev, python3/venv/pip, nodejs/npm, ripgrep, git, jq,
# zstd, openssh-client, gh, rustup stable minimal, docker CLI + buildx, uv, runner agent) plus
# libatomic1, Tauri/X11 build headers, docker-ce daemon and awscli (for the IMDS hook).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
  sudo ca-certificates curl gnupg unzip build-essential pkg-config libssl-dev \
  python3 python3-venv python3-pip nodejs npm ripgrep git jq zstd openssh-client \
  libatomic1 xvfb zsh clang \
  libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev libsoup-3.0-dev \
  libjavascriptcoregtk-4.1-dev libx11-dev libxdo-dev libxcb-shape0-dev libxcb-xfixes0-dev \
  libxkbcommon-dev patchelf file wget

# GitHub CLI
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
  > /etc/apt/sources.list.d/github-cli.list

# docker-ce + buildx + compose plugin (real daemon on the VM; isolation is the VM)
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y gh docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable docker

# AWS CLI v2 (hook uses it)
aws_dl=$(mktemp -d)
curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o "$aws_dl/awscli.zip"
unzip -q "$aws_dl/awscli.zip" -d "$aws_dl" && "$aws_dl/aws/install" && rm -rf "${aws_dl:?}"

# uv
curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh

# runner user: passwordless sudo (jobs need root: sysctl, unshare, apt) and docker group
id ubuntu >/dev/null
usermod -aG docker ubuntu
echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-runner && chmod 0440 /etc/sudoers.d/90-runner

# rustup stable (minimal) for the runner user
sudo -u ubuntu bash -c 'curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable'
echo 'export PATH="$HOME/.cargo/bin:$PATH"' > /etc/profile.d/cargo.sh
# Job steps get the runner process's PATH, not a login shell's, and sudo resets it, so ~/.cargo/bin is
# never on it (probe 2026-10-28: "rustup: command not found"). rustup's proxies dispatch on argv[0], so
# symlinks in /usr/local/bin work with RUSTUP_HOME/CARGO_HOME at their ~ubuntu defaults.
for b in /home/ubuntu/.cargo/bin/*; do ln -sf "$b" /usr/local/bin/; done
sudo -u ubuntu env -i HOME=/home/ubuntu PATH=/usr/local/bin:/usr/bin:/bin bash -c 'rustup --version && cargo --version' \
  || { echo "rustup/cargo not usable from a plain PATH" >&2; exit 1; }

# runner agent
install -d -o ubuntu -g ubuntu /opt/actions-runner
cd /opt/actions-runner
curl -fsSL -o runner.tgz "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
tar xzf runner.tgz && rm runner.tgz
chown -R ubuntu:ubuntu /opt/actions-runner
./bin/installdependencies.sh
echo 'PATH=/home/ubuntu/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' > /opt/actions-runner/.path

# IMDS hook + boot entrypoint (reviewed files, root-owned, not writable by the runner user).
GHR_FILES=/tmp/ghr-files  # no-tmp: ok — runner.pkr.hcl's upload dir on the throwaway packer builder VM
install -m 0755 -o root -g root "$GHR_FILES/ghr-imds-hook" /usr/local/sbin/ghr-imds-hook
install -m 0755 -o root -g root "$GHR_FILES/ghr-start" /usr/local/sbin/ghr-start

# bwrap-based tests (tests.yml, e2e-upgrade, e2e-desktop-update) need unprivileged user namespaces. Ubuntu 24.04
# restricts them via AppArmor; the old docker runners failed `sysctl kernel.apparmor_restrict_unprivileged_userns`
# with a read-only /proc/sys. Each job gets its own throwaway VM, so lift the restriction at boot and prove bwrap works.
DEBIAN_FRONTEND=noninteractive apt-get install -y bubblewrap
echo 'kernel.apparmor_restrict_unprivileged_userns = 0' > /etc/sysctl.d/60-ci-userns.conf
sysctl --system >/dev/null
[ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns)" = 0 ] || { echo "userns sysctl not applied" >&2; exit 1; }
sudo -u ubuntu bwrap --ro-bind / / --unshare-user --unshare-net true || { echo "bwrap unusable for ubuntu" >&2; exit 1; }

# amazon-ssm-agent (snap on Canonical AMIs) caches the instance-role credentials from IMDS at boot, before the
# hook runs; a root job could lift them from its memory. Remove it and prove it is gone.
snap remove --purge amazon-ssm-agent || true
if snap list amazon-ssm-agent >/dev/null 2>&1; then echo "amazon-ssm-agent still installed" >&2; exit 1; fi
if command -v amazon-ssm-agent >/dev/null 2>&1; then echo "amazon-ssm-agent binary still present" >&2; exit 1; fi

# Jobs install their own apt packages without `apt-get update` (js-tests, probe 2026-10-28: "Unable to locate
# package libnotify4"), as on GitHub-hosted images, so keep the package lists. cloud-init repoints apt at the boot
# region's EC2 mirror, which orphans lists baked in another region (us-east-1, 2026-10-29), so pin the global archive
# and tell cloud-init to leave the sources alone.
find /etc/apt/sources.list /etc/apt/sources.list.d -type f -exec sed -i -E 's#[a-z0-9-]+\.ec2\.archive\.ubuntu\.com#archive.ubuntu.com#g' {} +
printf 'apt:\n  preserve_sources_list: true\n' > /etc/cloud/cloud.cfg.d/99-ci-apt-sources.cfg
apt-get update
if grep -rq 'ec2\.archive\.ubuntu\.com' /etc/apt/sources.list /etc/apt/sources.list.d; then echo "regional mirror left in apt sources" >&2; exit 1; fi
[ "$(apt-cache policy libnotify4 | grep -c 'Candidate: [0-9]')" = 1 ] || { echo "package lists not usable" >&2; exit 1; }
# No appindicator package is baked: rust-tests installs libappindicator3-dev, which conflicts with ayatana's.
apt-get clean && rm -rf "${GHR_FILES:?}"

# Root steps here must not leave files in the runner user's home (probe 2026-10-28: a root-owned ~/.config broke
# browser-harness tests). Packer no longer passes HOME through sudo; fail the bake if anything still slipped in.
if find /home/ubuntu ! -user ubuntu | grep -q .; then
  find /home/ubuntu ! -user ubuntu >&2; echo "root-owned files in /home/ubuntu" >&2; exit 1
fi
cloud-init clean --logs
