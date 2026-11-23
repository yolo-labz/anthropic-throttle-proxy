FROM python:3.15.2-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9
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
