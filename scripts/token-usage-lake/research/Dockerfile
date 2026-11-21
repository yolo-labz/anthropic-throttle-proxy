FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app PORT=5000 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
    POLARS_MAX_THREADS=2 MLFLOW_DISABLE_AGENT_HINT=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock
COPY quarto.deb /tmp/quarto.deb
RUN echo '43678c160c3066b139ce30a741670bc5dc8d732fc6d320579f75bcace59397ae  /tmp/quarto.deb' | sha256sum -c -
RUN dpkg -i /tmp/quarto.deb && rm /tmp/quarto.deb
RUN groupadd --gid 10001 science && useradd --uid 10001 --gid science --home-dir /workspace science
COPY snapshots.py science.py desktop.py notebook.py report.qmd report.scss smoke.py entrypoint.sh Procfile ./
RUN chmod 755 /app/entrypoint.sh
USER science
EXPOSE 5000
CMD ["/app/entrypoint.sh", "web"]
