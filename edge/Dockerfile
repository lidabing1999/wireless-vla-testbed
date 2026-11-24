# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.5.1 AS uv
FROM nvidia/cuda:12.2.2-cudnn8-runtime-ubuntu22.04@sha256:2d913b09e6be8387e1a10976933642c73c840c0b735f0bf3c28d97fc9bc422e0
COPY --from=uv /uv /bin/uv
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential clang git git-lfs libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
ENV UV_PROJECT_ENVIRONMENT=/.venv PATH="/.venv/bin:$PATH"
RUN uv venv --python 3.11.9 /.venv
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG POLICY_WHEEL_ORIGIN=https://files.pythonhosted.org
ENV PIP_INDEX_URL=${PIP_INDEX_URL} POLICY_WHEEL_ORIGIN=${POLICY_WHEEL_ORIGIN}
COPY requirements.lock /tmp/policy.requirements
COPY resolve_requirements.py /tmp/resolve_requirements.py
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,source=wheels,target=/tmp/policy_wheels,readonly \
    uv pip install --python /.venv/bin/python pip==24.3.1 && \
    python /tmp/resolve_requirements.py && \
    python -m pip install --retries 8 --timeout 90 --no-deps --require-hashes -r /tmp/policy.resolved
COPY lerobot/ /tmp/lerobot/
COPY openpi/ /app/
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --no-deps /tmp/lerobot -e /app/packages/openpi-client -e /app && \
    python -m pip check && \
    python -c 'import shutil; from pathlib import Path; import transformers; shutil.copytree(Path("/app/src/openpi/models_pytorch/transformers_replace"), Path(transformers.__file__).parent, dirs_exist_ok=True)'
EXPOSE 8000
CMD ["/.venv/bin/python", "scripts/serve_policy.py", "--env", "LIBERO", "--port", "8000"]
