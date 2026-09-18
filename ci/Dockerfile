# The toolbox every pipeline step runs in.
#
# Built and pushed by .woodpecker/image.yaml whenever ci/ changes. Pinning the
# whole toolchain in one image is what makes a pipeline reproducible: a run from
# six months ago used exactly these versions, and nothing is downloaded from the
# internet in the middle of an apply.
FROM debian:13-slim

ARG TOFU_VERSION=1.9.0
ARG BWS_VERSION=1.0.0
ARG TARGETARCH=amd64

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ANSIBLE_CONFIG=/workspace/ansible/ansible.cfg

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg git jq unzip rsync openssh-client \
      netcat-openbsd \
      python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Tailscale, so a pipeline step can join the tailnet and reach hosts that have
# no public SSH. netcat-openbsd above is what carries SSH through the SOCKS
# proxy when the runner has no TUN device.
RUN curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg \
      -o /usr/share/keyrings/tailscale-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/debian trixie main" \
      > /etc/apt/sources.list.d/tailscale.list \
    && apt-get update && apt-get install -y --no-install-recommends tailscale \
    && rm -rf /var/lib/apt/lists/* \
    && tailscale version

# OpenTofu
RUN curl -fsSL "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_${TARGETARCH}.zip" -o /tmp/tofu.zip \
    && unzip -q /tmp/tofu.zip -d /tmp/tofu \
    && install -m 0755 /tmp/tofu/tofu /usr/local/bin/tofu \
    && rm -rf /tmp/tofu /tmp/tofu.zip \
    && tofu version

# Bitwarden Secrets Manager CLI
RUN curl -fsSL "https://github.com/bitwarden/sdk-sm/releases/download/bws-v${BWS_VERSION}/bws-x86_64-unknown-linux-gnu-${BWS_VERSION}.zip" -o /tmp/bws.zip \
    && unzip -q /tmp/bws.zip -d /tmp/bws \
    && install -m 0755 /tmp/bws/bws /usr/local/bin/bws \
    && rm -rf /tmp/bws /tmp/bws.zip \
    && bws --version

# Built with the repo root as the build context, so the collection list stays
# the single one in ansible/requirements.yml rather than a copy that drifts.
COPY ci/requirements.txt /tmp/requirements.txt
COPY ansible/requirements.yml /tmp/ansible-requirements.yml

RUN pip3 install --break-system-packages --no-cache-dir -r /tmp/requirements.txt \
    && ansible-galaxy collection install -r /tmp/ansible-requirements.yml -p /usr/share/ansible/collections

WORKDIR /workspace
