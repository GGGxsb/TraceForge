FROM node:22-bookworm-slim

ARG DEBIAN_MIRROR=http://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=http://deb.debian.org/debian-security

RUN sed -i "s#http://deb.debian.org/debian-security#${DEBIAN_SECURITY_MIRROR}#g; s#http://deb.debian.org/debian#${DEBIAN_MIRROR}#g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends bash git python3 ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /bin/bash runner \
    && mkdir -p /opt/traceforge /workspace \
    && chown -R runner:runner /workspace

RUN ln -s /usr/bin/python3 /usr/local/bin/python

COPY tool_worker.py /opt/traceforge/tool_worker.py

ENV HOME=/home/runner \
    PATH=/usr/local/bin:/usr/bin:/bin

USER runner
WORKDIR /workspace
CMD ["bash"]
