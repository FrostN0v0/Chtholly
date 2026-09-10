ARG BASE_IMAGE=ghcr.io/astral-sh/uv@sha256:a041b350d5d9483b538d5af07e9553ee8cbc7fc7fa90c2f7d20d93f18ce9bbd1
FROM ${BASE_IMAGE} AS dependencies

# Git is needed only for the existing locked entari-plugin-llm VCS dependency.
# Freeze Debian package resolution as well as Python and uv image provenance.
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260901T000000Z bookworm main\n' > /etc/apt/sources.list && \
    apt-get -o Acquire::Check-Valid-Until=false update && \
    apt-get install --no-install-recommends -y git ca-certificates && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /opt/workshop
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_NO_PROGRESS=1
COPY pyproject.toml uv.lock ./
COPY vendor/entari/ vendor/entari/
RUN uv sync --locked --python 3.10 --no-python-downloads --no-install-project --no-dev --all-extras
ARG FRAMEWORK_VERSION
RUN .venv/bin/python -c "import importlib.metadata, os, sys; assert sys.version_info[:2] == (3, 10); assert importlib.metadata.version('arclet-entari') == os.environ['FRAMEWORK_VERSION']"

FROM ${BASE_IMAGE} AS runtime
ARG FRAMEWORK_VERSION
ARG LOCK_SHA256
ARG WHEEL_SHA256
ARG INPUT_SHA256
ARG BASE_IMAGE
LABEL org.chtholly.workshop.protocol="1" \
      org.chtholly.workshop.python="3.10" \
      org.chtholly.workshop.framework="${FRAMEWORK_VERSION}" \
      org.chtholly.workshop.lock-sha256="${LOCK_SHA256}" \
      org.chtholly.workshop.wheel-sha256="${WHEEL_SHA256}" \
      org.chtholly.workshop.input-sha256="${INPUT_SHA256}" \
      org.chtholly.workshop.base="${BASE_IMAGE}"
COPY --from=dependencies /opt/workshop/.venv /opt/workshop/.venv
COPY worker/ /opt/workshop/worker/
# Runtime enforces a read-only root filesystem; copied files remain root-owned.
RUN mkdir /workspace && chmod 0755 /workspace
ENV HOME=/workspace TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
WORKDIR /workspace
USER 65532:65532
ENTRYPOINT ["/opt/workshop/.venv/bin/python", "-I", "-B", "/opt/workshop/worker/worker.py"]
