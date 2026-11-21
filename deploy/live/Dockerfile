FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src:/app
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock
COPY src /app/src
COPY scripts /app/scripts
COPY configs/live.base-eth-usdc.json configs/live.rpc.canary.json /app/configs/
USER 10001:10001
CMD ["python", "-m", "scripts.run_live_lp", "--mode", "live", "--readiness"]

FROM runtime AS validation
USER root
COPY requirements-dev.lock /app/requirements-dev.lock
RUN python -m pip install --no-cache-dir --require-hashes -r requirements-dev.lock
COPY tests /app/tests
COPY pyproject.toml /app/pyproject.toml
USER 10001:10001
