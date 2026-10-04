FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY native_topology_helpers.py container_probe.py /opt/topology-fixture/
ENV NATIVE_TOPOLOGY_FIXTURE=owned-disposable-test
CMD ["python3", "-c", "import time; time.sleep(1800)"]
