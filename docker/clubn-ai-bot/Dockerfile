# AMP-compatible image for the ClubÑ AI Bot template.
# Contains only the runtime: Python and a CPU-only Ollama. The bot code, its virtualenv and
# the model are installed into persistent instance storage (/AMP/clubn-ai-bot) by AMP Update.
ARG OLLAMA_VERSION=0.40.18
ARG GOSU_VERSION=1.20
FROM ollama/ollama:${OLLAMA_VERSION} AS ollama

# ampstart.sh drops privileges with gosu. Debian's gosu is built with an old Go whose standard library
# has known CVEs, so build the same upstream release with a current Go toolchain instead.
FROM golang:1.26-trixie AS gosu
ARG GOSU_VERSION
RUN git clone --quiet --depth 1 --branch "${GOSU_VERSION}" https://github.com/tianon/gosu /src/gosu \
    && cd /src/gosu \
    && CGO_ENABLED=0 go build -trimpath -ldflags '-s -w' -o /out/gosu . \
    && /out/gosu --version

# Must stay derived from ampbase: AMP Generic custom images need its /ampstart.sh entrypoint.
FROM cubecoders/ampbase:debian

USER root
# Upgrade the base image's packages (security fixes published after ampbase was built), install only
# what the scripts need and remove tools this instance never uses (git/git-lfs, Xvfb, wget, and the
# system pip: the bot's venv brings its own pip and AMP Update upgrades it).
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get -y upgrade \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        procps \
        python3 \
        python3-venv \
        tar \
    && DEBIAN_FRONTEND=noninteractive apt-get purge -y git git-lfs python3-pip xvfb xserver-common wget \
    && apt-get autoremove -y --purge \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY --from=gosu /out/gosu /usr/sbin/gosu
COPY --from=ollama /usr/bin/ollama /usr/bin/ollama
COPY --from=ollama /usr/lib/ollama /usr/lib/ollama
# The VM has no GPU: drop the GPU runtimes (several GB) and keep the CPU backends.
RUN find /usr/lib/ollama -mindepth 1 -maxdepth 1 -type d \
        \( -iname 'cuda*' -o -iname 'rocm*' -o -iname 'vulkan*' \) -exec rm -rf {} + \
    && test -x /usr/bin/ollama \
    && gosu nobody true

COPY docker/clubn-ai-bot/clubn-amp-update /usr/local/bin/clubn-amp-update
COPY docker/clubn-ai-bot/clubn-amp-run /usr/local/bin/clubn-amp-run
RUN chmod 0755 /usr/local/bin/clubn-amp-update /usr/local/bin/clubn-amp-run

ENTRYPOINT ["/ampstart.sh"]
CMD []
