FROM rust:1.85-slim-bookworm AS rust-builder
WORKDIR /build
COPY crates /build/crates
RUN cargo build --locked --manifest-path crates/Cargo.toml --release

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src:/app \
    OASIS_FASTPATH_LIB=/app/crates/target/release/liboasis_core.so
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock
COPY --from=rust-builder /build/crates/target/release/liboasis_core.so /app/crates/target/release/liboasis_core.so
COPY src /app/src
COPY scripts /app/scripts
COPY configs/live.rpc.canary.json /app/configs/live.rpc.canary.json
USER 10001:10001
CMD ["python", "-m", "scripts.run_base_arb_shadow", "--mode", "synthetic", "--ticks", "1", "--out", "/tmp/profit-shadow-readiness"]
