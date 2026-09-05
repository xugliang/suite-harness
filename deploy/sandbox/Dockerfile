# syntax=docker/dockerfile:1.7
# A digest-pinned base is mandatory, for example:
#   docker build --build-arg BASE_IMAGE=debian:bookworm-slim@sha256:<digest> ...
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        bash \
        ca-certificates \
        coreutils \
        findutils \
        grep \
        python3-minimal \
        ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 suiteharness \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin suiteharness

WORKDIR /workspace
USER 65532:65532
