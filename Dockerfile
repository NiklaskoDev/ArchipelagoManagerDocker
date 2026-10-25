# syntax=docker/dockerfile:1

# Archipelago Manager: web UI to host, generate and distribute Archipelago multiworlds.
#
#   docker build -t archipelago-manager .
#   docker build --build-arg AP_VERSION=0.6.8 -t archipelago-manager .   # pick the Archipelago release to bundle
ARG PYTHON_VERSION=3.12
ARG AP_VERSION=0.6.8

# ---------------------------------------------------------------------------------------------------------------------
# Stage 1: Archipelago itself, from source, with every world's requirements and the optional Cython speedups.
# ---------------------------------------------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS archipelago
ARG AP_VERSION
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# git: fetching the source and one world requirement that is installed from git. gcc/g++: native extensions. cmake:
# dolphin-memory-engine (The Wind Waker), which has no wheel for arm64 and is built from source there.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git gcc g++ libc6-dev cmake \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${AP_VERSION}" https://github.com/ArchipelagoMW/Archipelago.git /opt/archipelago \
 && rm -rf /opt/archipelago/.git

WORKDIR /opt/archipelago

# The server is headless, so skip the GUI toolkits (kivy, kivymd, pyshortcuts). Worlds bring their own requirements;
# the core file doubles as a constraint so a world cannot change what the core pinned. `requests` and a setuptools
# that still ships pkg_resources are imported by a few worlds without being declared anywhere.
RUN python -m venv /opt/venv-ap \
 && grep -v -i -E '^(kivy|kivymd|pyshortcuts)' requirements.txt > /tmp/core-requirements.txt \
 && /opt/venv-ap/bin/pip install -r /tmp/core-requirements.txt "setuptools>=75,<81" requests \
 && for requirements in worlds/*/requirements.txt; do \
        /opt/venv-ap/bin/pip install -r "$requirements" --constraint /tmp/core-requirements.txt || exit 1; \
    done

# Optional but worthwhile: Archipelago falls back to pure Python without this, which is slower for large multiworlds.
RUN /opt/venv-ap/bin/cythonize -b -i _speedups.pyx

# Archipelago expects a Players folder and a manifest.json in its install directory when that directory is read-only
# for the running user, which is how this image runs. Without them every start fails with FileNotFoundError.
RUN mkdir -p Players custom_worlds \
 && printf '{"version": "%s"}\n' "${AP_VERSION}" > manifest.json \
 && /opt/venv-ap/bin/python -m compileall -q /opt/archipelago /opt/venv-ap/lib

# ---------------------------------------------------------------------------------------------------------------------
# Stage 2: the manager's own, much smaller, environment.
# ---------------------------------------------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS manager
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY manager/requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv-manager \
 && /opt/venv-manager/bin/pip install -r /tmp/requirements.txt

# ---------------------------------------------------------------------------------------------------------------------
# Final image
# ---------------------------------------------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm
ARG AP_VERSION
ARG VERSION=dev

LABEL org.opencontainers.image.title="Archipelago Manager" \
      org.opencontainers.image.description="Web UI to host, generate and distribute Archipelago multiworld randomizers" \
      org.opencontainers.image.source="https://github.com/NiklaskoDev/ArchipelagoManagerDocker" \
      org.opencontainers.image.version="${VERSION}" \
      io.archipelago.version="${AP_VERSION}"

# tini reaps the processes the manager starts and passes SIGTERM on. tzdata makes the TZ setting work (the slim image
# has none). The entrypoint drops root with util-linux's setpriv, which the base image already has.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY --from=archipelago /opt/archipelago /opt/archipelago
COPY --from=archipelago /opt/venv-ap /opt/venv-ap
COPY --from=manager /opt/venv-manager /opt/venv-manager
COPY manager /app/manager
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
 && /opt/venv-manager/bin/python -m compileall -q /app/manager

ENV DATA_DIR=/config \
    AP_DIR=/opt/archipelago \
    AP_PYTHON=/opt/venv-ap/bin/python \
    PORT=8080 \
    PORT_RANGE=38281-38300 \
    PUID=99 \
    PGID=100 \
    UMASK=022 \
    SKIP_REQUIREMENTS_UPDATE=true \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv-manager/bin:$PATH"

WORKDIR /app/manager
VOLUME /config
EXPOSE 8080 38281-38300

# Healthy as soon as the web UI answers, even if Archipelago itself has a problem: the UI is where that is shown.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '8080'), timeout=4)"

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["python", "-m", "archipelago_manager"]
