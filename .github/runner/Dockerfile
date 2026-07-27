# Docker-based GitHub Actions self-hosted runner for Flashlight CI/CD.
#
# Note: this container is *CI infrastructure*, not the app. Flashlight itself ships
# as a pip wheel with no Docker; this image only hosts the Actions runner agent
# (with uv + git preinstalled so the gate runs fast).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq sudo tar gzip \
    && rm -rf /var/lib/apt/lists/*

# uv (and uvx) straight from the official image — provides Python on demand too.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# The Actions runner refuses to run as root; create an unprivileged user with
# passwordless sudo (installdependencies.sh needs it).
RUN useradd -m -s /bin/bash runner \
    && echo "runner ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/runner

USER runner
WORKDIR /home/runner

# Pull the latest runner release at build time and install its OS deps.
RUN set -eux; \
    version="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest \
        | jq -r .tag_name | sed 's/^v//')"; \
    curl -fsSL -o runner.tar.gz \
        "https://github.com/actions/runner/releases/download/v${version}/actions-runner-linux-x64-${version}.tar.gz"; \
    tar xzf runner.tar.gz; \
    rm runner.tar.gz; \
    sudo ./bin/installdependencies.sh

COPY --chown=runner:runner entrypoint.sh /home/runner/entrypoint.sh
RUN chmod +x /home/runner/entrypoint.sh

ENTRYPOINT ["/home/runner/entrypoint.sh"]
