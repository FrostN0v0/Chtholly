# uv 0.9.30-python3.11-bookworm-slim, pinned to its official image index.
ARG BASE_IMAGE=ghcr.io/astral-sh/uv@sha256:4f5d923c9dcea037f57bda425dd209f3ec643da2f0b74227f68d09dab0b3bb36
FROM ${BASE_IMAGE} AS dependencies

WORKDIR /opt/workshop
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_NO_PROGRESS=1
COPY pyproject.toml uv.lock ./
COPY vendor/ vendor/
RUN uv sync --locked --python 3.11 --no-python-downloads --no-install-project --no-dev --all-extras
ARG FRAMEWORK_VERSION
RUN .venv/bin/python -c "import importlib.metadata, os, sys; assert sys.version_info[:2] == (3, 11); assert importlib.metadata.version('arclet-entari') == os.environ['FRAMEWORK_VERSION']"
# The locked Playwright wheel selects its exact Chromium revision at image build.
# Pillow is worker-only: pin the CPython 3.11 Linux wheel independently of host deps.
RUN uv pip install --python .venv/bin/python --no-deps 'https://files.pythonhosted.org/packages/f2/2f/d7675ecae6c43e9f12aa8d58b6012683b20b6edfbdac7abcb4e6af7a3784/pillow-11.3.0-cp311-cp311-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl#sha256=106064daa23a745510dabce1d84f29137a37224831d88eb4ce94bb187b1d7e5f'
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
      org.chtholly.workshop.python="3.11" \
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
