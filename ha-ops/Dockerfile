ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM $BUILD_FROM

RUN apk add --no-cache bash curl diffutils git openssh-client openssh-keygen py3-pip py3-yaml python3 rsync \
    && pip3 install --no-cache-dir --break-system-packages rfc8785==0.1.4

WORKDIR /app

COPY run.sh /run.sh
COPY app /app
COPY config.yaml /app/config.yaml

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
