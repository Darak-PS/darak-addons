# Current Supervisor ignores build.yaml and builds from its Alpine base
# without Python, so the multi-arch Python base is pinned here.
FROM ghcr.io/home-assistant/base-python:3.13-alpine3.24-2026.08.0@sha256:cf7bc2e449577ab9b28ddf9f994cec2d77f3a6539de6eb5111c141e059f864ca
ARG BUILD_ARCH

LABEL io.hass.type="addon" \
      io.hass.arch="${BUILD_ARCH}" \
      io.hass.version="0.1.54"

RUN apk add --no-cache curl \
 && case "$BUILD_ARCH" in \
      aarch64) ts_arch=arm64; ts_url=https://pkgs.tailscale.com/stable/tailscale_1.102.4_arm64.tgz; ts_sha=9dd1e6a592a014bbaea0103167ffe299adeda4ba14e078ce9c2895364f6c4c3f ;; \
      amd64) ts_arch=amd64; ts_url=https://pkgs.tailscale.com/stable/tailscale_1.102.4_amd64.tgz; ts_sha=50748df1045e60b5b695f19f4c56b0da36c019948b440fb456b6584a50f0d8b9 ;; \
      *) echo "Unsupported build architecture: $BUILD_ARCH" >&2; exit 1 ;; \
    esac \
 && curl -fsSL "$ts_url" -o /tmp/tailscale.tgz \
 && printf '%s  %s\n' "$ts_sha" /tmp/tailscale.tgz | sha256sum -c - \
 && tar -xzf /tmp/tailscale.tgz --strip-components=1 -C /usr/local/bin \
      tailscale_1.102.4_${ts_arch}/tailscale tailscale_1.102.4_${ts_arch}/tailscaled \
 && rm /tmp/tailscale.tgz

COPY agent/requirements.txt /opt/darak-agent/requirements.txt
RUN pip install --no-cache-dir -r /opt/darak-agent/requirements.txt
COPY agent/darak_agent /opt/darak-agent/darak_agent
COPY integrations/darak_voice /opt/darak-agent/integrations/darak_voice
COPY integrations/darak_oauth /opt/darak-agent/integrations/darak_oauth
ENV PYTHONPATH=/opt/darak-agent
RUN mkdir -p /var/run/tailscale

# The base image runs CMD under s6 /init, which clears the environment;
# with-contenv restores SUPERVISOR_TOKEN and PYTHONPATH.
CMD ["/usr/bin/with-contenv", "python3", "-m", "darak_agent"]
