# AMP-compatible image for the ClubÑ AI Bot template.
# Contains only the runtime: Python and a CPU-only Ollama. The bot code, its virtualenv and
# the model are installed into persistent instance storage (/AMP/clubn-ai-bot) by AMP Update.
ARG OLLAMA_VERSION=0.40.13
FROM ollama/ollama:${OLLAMA_VERSION} AS ollama

# Must stay derived from ampbase: AMP Generic custom images need its /ampstart.sh entrypoint.
FROM cubecoders/ampbase:debian

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        procps \
        python3 \
        python3-pip \
        python3-venv \
        tar \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ollama /usr/bin/ollama /usr/bin/ollama
COPY --from=ollama /usr/lib/ollama /usr/lib/ollama
# The VM has no GPU: drop the GPU runtimes (several GB) and keep the CPU backends.
RUN find /usr/lib/ollama -mindepth 1 -maxdepth 1 -type d \
        \( -iname 'cuda*' -o -iname 'rocm*' -o -iname 'vulkan*' \) -exec rm -rf {} + \
    && test -x /usr/bin/ollama

COPY docker/clubn-ai-bot/clubn-amp-update /usr/local/bin/clubn-amp-update
COPY docker/clubn-ai-bot/clubn-amp-run /usr/local/bin/clubn-amp-run
RUN chmod 0755 /usr/local/bin/clubn-amp-update /usr/local/bin/clubn-amp-run

ENTRYPOINT ["/ampstart.sh"]
CMD []
