ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache bash curl git openssh-client openssh-keygen python3 rsync

WORKDIR /app

COPY run.sh /run.sh
COPY app /app

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
