# syntax=docker/dockerfile:1
#
# TreeAudit images (docs/DECISIONS.md D-017, D-019).
#   dev : test and quality tools; the repository is mounted at /app at run time.
#   app : minimal runtime image, no uv and no development dependencies.
#
# Base images are pinned by digest. Update tag and digest together.
ARG PYTHON_IMAGE=python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH
WORKDIR /app

# ---- uvtool: Python + uv, used to lock and install dependencies
FROM base AS uvtool
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

# ---- dev
FROM uvtool AS dev
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --group dev
# Sources come from the bind-mounted repository, not from the image.
ENV PYTHONPATH=/app/src
CMD ["pytest"]

# ---- build: runtime dependencies and the project itself
FROM uvtool AS build
ENV UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# ---- app
FROM base AS app
# Defense in depth: no-new-privileges already neutralizes setuid/setgid binaries
# (su, mount, passwd...); strip the bits so they stay harmless without it.
RUN find / -xdev -type f -perm /6000 -exec chmod a-s {} +
COPY --from=build /opt/venv /opt/venv
COPY docker/config.toml /etc/treeaudit/config.toml
ENV TREEAUDIT_CONFIG=/etc/treeaudit/config.toml \
    TREEAUDIT_CONTAINER=1
# Compose overrides this with the host user's UID/GID. Never root.
USER 65534:65534
EXPOSE 8765
ENTRYPOINT ["treeaudit"]
CMD ["serve"]
