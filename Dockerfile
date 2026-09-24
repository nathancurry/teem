FROM python:3.11-slim-bookworm AS build

RUN pip install --no-cache-dir uv==0.12.2
ENV UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY teem ./teem
RUN uv sync --locked --no-dev --no-editable

FROM python:3.11-slim-bookworm AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}"
WORKDIR /app
USER 568:568
ENTRYPOINT ["teem-server"]

# Build this target only when local dictation is enabled. The runner and model
# remain read-only files supplied by the deployment configuration mount.
FROM runtime AS speech
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg bubblewrap \
    && rm -rf /var/lib/apt/lists/*
USER 568:568

FROM runtime AS server
