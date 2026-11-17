FROM ghcr.io/home-assistant/base:3.24-2026.08.0@sha256:93ef607824e3f27e868f11b10938283a98bf880ed57bcf8eaa81c6c2d521f6f5
ARG BUILD_ARCH=aarch64

COPY runtime-install.sh runtime-apk-*.lock runtime-pip.lock /tmp/
RUN /bin/sh /tmp/runtime-install.sh "$BUILD_ARCH"

WORKDIR /app

COPY run.sh /run.sh
COPY app /app
COPY config.yaml /app/config.yaml
COPY runtime-profile.json /usr/share/ha-ops/runtime-profile.json

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
