FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2 AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 \
    /uv /uvx /bin/

WORKDIR /app

ENV \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1

COPY .python-version ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv python install

# Dependencies first: this layer only changes when the lockfile does.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

COPY src/ src/

# The repository is not mounted, so the version arrives as a build argument (computed
# on the host). Without it the package is 0.0.0.
ARG VERSION=""
RUN --mount=type=cache,target=/root/.cache/uv \
    SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION:-0.0.0} \
    uv sync --frozen --no-editable

# Trim what a running app never needs.
RUN PYTHON_DIR="$(find /opt/python -mindepth 1 -maxdepth 1 -type d -name 'cpython-*' -print -quit)" \
    && STDLIB="$(find "${PYTHON_DIR}/lib" -mindepth 1 -maxdepth 1 -type d -name 'python3.*' -print -quit)" \
    && rm -rf \
        "${PYTHON_DIR}/include" \
        "${PYTHON_DIR}/lib/tcl"* \
        "${PYTHON_DIR}/lib/tk"* \
        "${PYTHON_DIR}/lib/libtcl"* \
        "${PYTHON_DIR}/lib/libtk"* \
        "${STDLIB}/idlelib" \
        "${STDLIB}/tkinter" \
        "${STDLIB}/turtledemo" \
        "${STDLIB}/ensurepip" \
        "${STDLIB}/site-packages/pip" \
        "${STDLIB}/site-packages/setuptools" \
    && find /opt/python /app/.venv \
        \( -type d -name 'tests' -o -type d -name 'test' \) -prune -exec rm -rf '{}' + \
    && find /opt/python /app/.venv \
        \( -name '*.pyo' -o -name '*.a' \) -delete

# libgcc_s and libstdc++ are needed by greenlet's C extension, and distroless does not
# ship them. Mirror them wherever this build's libc multiarch directory is.
RUN for lib in libgcc_s.so.1 libstdc++.so.6; do \
        src="$(find /usr/lib -maxdepth 2 -name "${lib}" -print -quit)"; \
        [ -n "${src}" ] || { echo "missing ${lib}" >&2; exit 1; }; \
        dest="/opt/runtime-libs${src#/usr/lib}"; \
        mkdir -p "$(dirname "${dest}")"; \
        cp "${src}" "${dest}"; \
    done

COPY migrations/ /app/migrations/
RUN mkdir -p /data /backups && chown 1000:1000 /data /backups


FROM gcr.io/distroless/base-debian13:nonroot@sha256:9039a88709bcc2bacf5a2faac331d65dd6840f763f225ad9594cc0c67bcd0fba AS runtime

ARG VERSION=""
LABEL org.opencontainers.image.title="CastTrail" \
      org.opencontainers.image.description="Tracks people, films and series, and finds UK convention guest appearances." \
      org.opencontainers.image.source="https://github.com/samcorky/casttrail" \
      org.opencontainers.image.version="${VERSION}"

COPY --from=builder /opt/runtime-libs/ /usr/lib/

WORKDIR /app

ENV \
    PATH="/opt/python/bin:/app/.venv/bin" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CASTTRAIL_MIGRATIONS_DIR=/app/migrations \
    CASTTRAIL_DATABASE_URL=sqlite+aiosqlite:////data/casttrail.db \
    CASTTRAIL_BACKUP_DIR=/backups

COPY --from=builder /opt/python /opt/python
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/migrations /app/migrations
COPY scripts/healthcheck.py /app/healthcheck.py
COPY --from=builder --chown=1000:1000 /data /data
COPY --from=builder --chown=1000:1000 /backups /backups

EXPOSE 8000

USER 1000:1000

HEALTHCHECK \
    --interval=30s \
    --timeout=5s \
    --start-period=10s \
    --retries=3 \
    CMD ["/app/.venv/bin/python", "/app/healthcheck.py"]

# One worker: the app keeps its timers and Rostercon spacing in memory. Keep-alive is
# off because idle connections were being reset behind the Tailscale proxy.
ENTRYPOINT ["/app/.venv/bin/granian"]
CMD ["--interface", "asgi", "--factory", "--no-http1-keep-alive", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "casttrail.main:create_app"]
