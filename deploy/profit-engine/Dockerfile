FROM rust:1.100-slim-bookworm@sha256:7c7d4eb4d05b7781187a327809466cfddf29753f6defdc3d19a0e291951b7a37 AS rust-builder
WORKDIR /build
COPY crates /build/crates
RUN cargo build --locked --manifest-path crates/Cargo.toml --release

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS runtime
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
COPY configs/profit-engine-risk.example.json /app/configs/profit-engine-risk.example.json
USER 10001:10001
CMD ["python", "-m", "scripts.run_base_arb_shadow", "--mode", "synthetic", "--ticks", "1", "--out", "/tmp/profit-shadow-readiness"]
