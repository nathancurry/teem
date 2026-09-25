# Image for implementer, check, and adversary containers. Pin CLI versions and
# rebuild deliberately; the worker records the image tag in its policy file.
FROM docker.io/library/node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash ca-certificates git python3 python3-venv python3-pip ripgrep build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code@2.1.282 @openai/codex@0.157.0 \
    && npm cache clean --force
COPY teem-implement /usr/local/bin/teem-implement
