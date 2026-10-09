FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db
ARG GIT_DEB_VERSION=1:2.39.5-0+deb12u3
ARG RSYNC_DEB_VERSION=3.2.7-1+deb12u6
ARG ETCKEEPER_DEB_VERSION=1.18.20-1
ARG METASTORE_DEB_VERSION=1.1.2-4+b1
ARG ACL_DEB_VERSION=2.3.1-3
ARG LIBBSD_DEB_VERSION=0.11.7-2
# Extract etckeeper without running its maintainer scripts or initializing /etc.
RUN apt-get update \
    && apt-cache show "etckeeper=${ETCKEEPER_DEB_VERSION}" >/dev/null \
    && apt-get install -y --no-install-recommends "git=${GIT_DEB_VERSION}" "rsync=${RSYNC_DEB_VERSION}" \
       "metastore=${METASTORE_DEB_VERSION}" "acl=${ACL_DEB_VERSION}" \
       "libacl1=${ACL_DEB_VERSION}" "libbsd0=${LIBBSD_DEB_VERSION}" \
    && mkdir -p /opt/metadata-tools /tmp/etckeeper-package \
    && cd /tmp/etckeeper-package \
    && apt-get download "etckeeper=${ETCKEEPER_DEB_VERSION}" \
    && dpkg-deb --extract etckeeper_*.deb /opt/metadata-tools \
    && dpkg-deb --field etckeeper_*.deb Version > /opt/metadata-tools/etckeeper.version \
    && rm -rf /tmp/etckeeper-package \
    && rm -rf /var/lib/apt/lists/*
ENV PATH="/opt/metadata-tools/usr/bin:${PATH}"
ENV ETCKEEPER_CONF_DIR=/opt/metadata-tools/etc/etckeeper
COPY requirements-dev.txt /opt/ha-ops-dev/requirements-dev.txt
COPY toolchain.json /opt/ha-ops-dev/toolchain.json
RUN python3 -m pip install --no-cache-dir -r /opt/ha-ops-dev/requirements-dev.txt
COPY native_topology_helpers.py container_probe.py /opt/topology-fixture/
ENV NATIVE_TOPOLOGY_FIXTURE=owned-disposable-test
CMD ["python3", "-c", "import time; time.sleep(1800)"]
