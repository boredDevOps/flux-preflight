# flux-preflight: runs in GitLab CI or GitHub Actions on changes to a Flux repository.
# The binaries come from their projects' own images, pinned by digest.
FROM ghcr.io/fluxcd/flux-cli:v2.7.2@sha256:c10e77794dc4519322cbc58e75c7f612125e4276b726c6cb019d87e9dadb08dd AS flux
FROM ghcr.io/yannh/kubeconform:v0.7.0@sha256:85dbef6b4b312b99133decc9c6fc9495e9fc5f92293d4ff3b7e1b30f5611823c AS kubeconform

FROM python:3.12-slim-bookworm AS base
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system safe.directory '*'
COPY --from=flux /usr/local/bin/flux /usr/local/bin/flux
COPY --from=kubeconform /kubeconform /usr/local/bin/kubeconform
WORKDIR /opt/flux-preflight
COPY pyproject.toml LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
COPY flux_preflight ./flux_preflight

# Lint, types and tests, with the same flux and kubeconform the image ships.
FROM base AS test
RUN pip install --no-cache-dir ".[dev]"
COPY tests ./tests
RUN git config --global user.email ci@example.invalid && git config --global user.name ci \
    && ruff check . && ruff format --check . && mypy flux_preflight && pytest -q

FROM base
RUN pip install --no-cache-dir .
# `source` links the GHCR package to the repository; it must be there on the first push.
LABEL org.opencontainers.image.source="https://github.com/boredDevOps/flux-preflight" \
      org.opencontainers.image.description="Checks a Flux repository's merge request for what it would delete or break in the cluster" \
      org.opencontainers.image.licenses="Apache-2.0"
# The CI runs its own commands in the image, in its own clone, so no ENTRYPOINT.
CMD ["flux-preflight", "--help"]
