FROM cubecoders/ampbase:debian

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gcc \
        g++ \
        git \
        libffi-dev \
        libolm-dev \
        make \
        nodejs \
        npm \
        python3 \
        python3-dev \
        python3-pip \
        python3-venv \
        procps \
        supervisor \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY docker/hermes-amp-update /usr/local/bin/hermes-amp-update
COPY docker/hermes-amp-run /usr/local/bin/hermes-amp-run
COPY docker/hermes-agent.conf /etc/supervisor/conf.d/hermes-agent.conf
RUN chmod 0755 /usr/local/bin/hermes-amp-update /usr/local/bin/hermes-amp-run

ENTRYPOINT ["/ampstart.sh"]
CMD []
