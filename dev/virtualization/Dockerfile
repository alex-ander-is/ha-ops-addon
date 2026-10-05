FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db
ARG GIT_DEB_VERSION=1:2.39.5-0+deb12u3
RUN apt-get update && apt-get install -y --no-install-recommends "git=${GIT_DEB_VERSION}" \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-dev.txt /opt/ha-ops-dev/requirements-dev.txt
COPY toolchain.json /opt/ha-ops-dev/toolchain.json
RUN python3 -m pip install --no-cache-dir -r /opt/ha-ops-dev/requirements-dev.txt
COPY native_topology_helpers.py container_probe.py /opt/topology-fixture/
ENV NATIVE_TOPOLOGY_FIXTURE=owned-disposable-test
CMD ["python3", "-c", "import time; time.sleep(1800)"]
