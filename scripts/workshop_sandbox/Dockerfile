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
COPY vendor/ vendor/
RUN uv sync --locked --python 3.10 --no-python-downloads --no-install-project --no-dev --all-extras
ARG FRAMEWORK_VERSION
RUN .venv/bin/python -c "import importlib.metadata, os, sys; assert sys.version_info[:2] == (3, 10); assert importlib.metadata.version('arclet-entari') == os.environ['FRAMEWORK_VERSION']"
# The locked Playwright wheel selects its exact Chromium revision at image build.
# Pillow is worker-only: pin the CPython 3.10 Linux wheel independently of host deps.
RUN uv pip install --python .venv/bin/python --no-deps 'https://files.pythonhosted.org/packages/f0/16/1a6bf01fb622fb9cf5c91683823f073f053005c849b1f52ed613afcf8dae/pillow-11.3.0-cp310-cp310-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl#sha256=4445fa62e15936a028672fd48c4c11a66d641d2c05726c7ec1f8ba6a572036ae'
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/workshop/browsers
RUN .venv/bin/python -m playwright install chromium && chmod -R a-w /opt/workshop/browsers

FROM ${BASE_IMAGE} AS runtime
ARG FRAMEWORK_VERSION
ARG LOCK_SHA256
ARG WHEEL_SHA256
ARG INPUT_SHA256
ARG BASE_IMAGE
LABEL org.chtholly.workshop.protocol="1" \
      org.chtholly.workshop.rendering="playwright-v1" \
      org.chtholly.workshop.egress="open-meteo-v1" \
      org.chtholly.workshop.python="3.10" \
      org.chtholly.workshop.framework="${FRAMEWORK_VERSION}" \
      org.chtholly.workshop.lock-sha256="${LOCK_SHA256}" \
      org.chtholly.workshop.wheel-sha256="${WHEEL_SHA256}" \
      org.chtholly.workshop.input-sha256="${INPUT_SHA256}" \
      org.chtholly.workshop.base="${BASE_IMAGE}"
COPY --from=dependencies /opt/workshop/.venv /opt/workshop/.venv
COPY --from=dependencies /opt/workshop/browsers /opt/workshop/browsers
# Use the same immutable Debian snapshot for Chromium libraries and local fonts.
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260901T000000Z bookworm main\n' > /etc/apt/sources.list && \
    apt-get -o Acquire::Check-Valid-Until=false update && \
    apt-get install --no-install-recommends -y ca-certificates fontconfig fonts-inter fonts-noto-cjk \
      libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcairo2 libcups2 \
      libdbus-1-3 libdrm2 libgbm1 libglib2.0-0 libnspr4 libnss3 libpango-1.0-0 \
      libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 \
      libxrandr2 && \
    fc-cache -f && rm -rf /var/lib/apt/lists/*
COPY worker/ /opt/workshop/worker/
# Runtime enforces a read-only root filesystem; copied files remain root-owned.
RUN mkdir /workspace && chmod 0755 /workspace
ENV HOME=/workspace TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
ENV PLAYWRIGHT_BROWSERS_PATH=/tmp/workshop-playwright/browsers XDG_CACHE_HOME=/tmp/cache XDG_CONFIG_HOME=/tmp/config
WORKDIR /workspace
USER 65532:65532
ENTRYPOINT ["/opt/workshop/.venv/bin/python", "-I", "-B", "/opt/workshop/worker/worker.py"]
