# Base pinned by multi-arch index digest (python:3.12-slim, resolved 2026-11-06):
# python@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

WORKDIR /tmp/build
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && rm -rf /tmp/build \
    && groupadd --gid 10001 peh \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin peh \
    && mkdir /work \
    && chown 10001:10001 /work

COPY runner.py /opt/peh/runner.py
RUN chmod 0555 /opt/peh/runner.py

USER 10001:10001
WORKDIR /work
ENTRYPOINT ["python","-I","/opt/peh/runner.py"]
