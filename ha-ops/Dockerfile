ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM $BUILD_FROM

RUN apk add --no-cache bash curl git openssh-client openssh-keygen python3 rsync

WORKDIR /app

COPY run.sh /run.sh
COPY app /app
COPY config.yaml /app/config.yaml

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
