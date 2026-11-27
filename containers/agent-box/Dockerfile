# agent-box: one long-lived pod where Herdr Core keeps terminal sessions alive
# and Claude Code runs inside them. Clients reach it over SSH (port 2222).
# The home directory is a persistent volume; this image only supplies the OS
# layer and a pinned seed copy of herdr-core. See README.md for the contract.

# --- fetch: pinned, checksum-verified downloads ------------------------------
FROM debian:trixie-slim@sha256:8aa5a7bf5a1955bddbe7d74fcdd0b91ab30e74362a8b73af672b0481b35b44a0 AS fetch

ARG DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /fetch

# Herdr Core (Apache-2.0): a single static binary. Only the seed lives in the
# image; the entrypoint copies it to ~/.local/bin, where Herdr updates it.
ARG HC_VERSION=0.1.22
ARG HC_SHA256=3054ec3992ce80c5715d721caa9dcefa6056721d15dc113f4750afce763ba5df
RUN curl -fsSLo herdr-core.tar.gz "https://releases.herdr.dev/core/${HC_VERSION}/herdr-core-linux-x86_64.tar.gz" \
    && echo "${HC_SHA256}  herdr-core.tar.gz" | sha256sum -c - \
    && mkdir herdr-core \
    && tar -xzf herdr-core.tar.gz -C herdr-core --no-same-owner \
    && test "$(herdr-core/herdr-core --version)" = "hc ${HC_VERSION}"

# mise (MIT): the checksum is this archive's entry in the release's
# GPG-signed SHASUMS256.asc (release key 24853EC9F655CE80B48E6C3A8B81C9D17413A06D).
ARG MISE_VERSION=2026.11.10
ARG MISE_SHA256=daf0f8d8856ba1017b8760196c4cdbd2a3c6aa554839eeed31c389ec22d86479
RUN curl -fsSLo mise.tar.xz "https://github.com/jdx/mise/releases/download/v${MISE_VERSION}/mise-v${MISE_VERSION}-linux-x64.tar.xz" \
    && echo "${MISE_SHA256}  mise.tar.xz" | sha256sum -c - \
    && tar -xJf mise.tar.xz --no-same-owner \
    && mise/bin/mise --version

# --- image -------------------------------------------------------------------
FROM debian:trixie-slim@sha256:8aa5a7bf5a1955bddbe7d74fcdd0b91ab30e74362a8b73af672b0481b35b44a0

ARG DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Herdr records /etc/machine-id in its state and takes a new identity (and
# forgets its terminals) when it changes, so the image must not carry one:
# no systemd, no dbus, and the file is checked after every apt step.
# openssh-server depends on "systemd | systemd-standalone-sysusers | ...";
# naming the standalone sysusers tool keeps apt from choosing full systemd.
# openssh-server's postinst generates host keys; they would be public in a
# public image, and sshd uses a key kept in the home volume instead.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        bash-completion \
        bind9-dnsutils \
        build-essential \
        bzip2 \
        ca-certificates \
        curl \
        fd-find \
        file \
        gh \
        git \
        git-lfs \
        gnupg \
        iproute2 \
        iputils-ping \
        jq \
        less \
        nano \
        netcat-openbsd \
        openssh-client \
        openssh-server \
        pkg-config \
        procps \
        psmisc \
        python3 \
        python3-pip \
        python3-venv \
        ripgrep \
        rsync \
        systemd-standalone-sysusers \
        tini \
        tree \
        tzdata \
        unzip \
        vim-tiny \
        wget \
        xz-utils \
        zip \
        zstd \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/ssh/ssh_host_* /etc/machine-id /var/lib/dbus/machine-id \
    && for p in systemd dbus dbus-daemon; do \
        if dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'ok installed'; then \
            echo "$p must not be installed" >&2; exit 1; \
        fi; \
    done

# The agent user. /home/agent exists here for runs without a volume; in the pod
# a volume at /home hides it and the entrypoint creates it. The password is
# "*" (no password, not locked), so key logins work whatever reads shadow.
RUN groupadd --gid 1000 agent \
    && useradd --uid 1000 --gid agent --shell /bin/bash --home-dir /home/agent \
        --create-home --password '*' agent \
    && chmod 0750 /home/agent \
    && install -d -m 0755 /run/sshd /opt/agent-box /usr/local/etc/agent-box \
    && install -d -m 0755 -o agent -g agent /run/agent-box \
    && rm -f /etc/machine-id \
    && test ! -e /etc/machine-id

# Not on any PATH: a second herdr-core there would shadow Herdr's own updates,
# which always write ~/.local/bin.
COPY --from=fetch --chmod=0755 /fetch/herdr-core/herdr-core /opt/agent-box/herdr-core
COPY --from=fetch --chmod=0644 /fetch/herdr-core/LICENSE /fetch/herdr-core/THIRD-PARTY-NOTICES /usr/share/doc/herdr-core/
COPY --from=fetch --chmod=0755 /fetch/mise/bin/mise /usr/local/bin/mise
COPY --from=fetch --chmod=0644 /fetch/mise/LICENSE /usr/share/doc/mise/LICENSE
COPY --chmod=0644 mise-system.toml /etc/mise/config.toml

COPY --chmod=0755 agent-box-entrypoint agent-box-env agent-box-ready agent-box-stop agent-launch /usr/local/bin/
COPY --chmod=0644 sshd_config /usr/local/etc/agent-box/sshd_config
COPY --chmod=0644 profile.sh /etc/profile.d/agent-box.sh

# PATH here is only for `docker exec` / `kubectl exec` convenience: the core,
# and so every terminal, gets its environment from agent-box-env.
ENV HOME=/home/agent \
    LANG=C.UTF-8 \
    PATH=/home/agent/.local/bin:/home/agent/.local/share/mise/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

USER 1000:1000
EXPOSE 2222

# tini is PID 1 and reaps the orphans Herdr leaves behind; the entrypoint is
# the supervisor (never herdr-core itself, which exits on every update).
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/agent-box-entrypoint"]
