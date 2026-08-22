# syntax=docker/dockerfile:1.7

FROM ghcr.io/openai/tunnel-client:v0.0.12 AS tunnel-client

FROM ghcr.io/home-assistant/base:3.22

ARG BUILD_ARCH
ARG BUILD_VERSION=dev

LABEL \
    io.hass.name="Home Assistant Observer for ChatGPT" \
    io.hass.description="Safe diagnostics and opt-in YAML management through an OpenAI Secure MCP Tunnel" \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.arch="${BUILD_ARCH}"

RUN apk add --no-cache \
        python3 \
        py3-pip \
        py3-virtualenv \
    && python3 -m venv /opt/venv

COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir --requirement /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY --from=tunnel-client /usr/bin/tunnel-client /usr/bin/tunnel-client
COPY rootfs /
COPY run.sh /run.sh
RUN chmod 0755 /run.sh

WORKDIR /app
CMD ["/run.sh"]
