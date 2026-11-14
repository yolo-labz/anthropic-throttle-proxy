#!/bin/sh
set -eu
export PYTHONPATH=/app
case "${1:-web}" in
  web)
    mkdir -p /workspace/notebooks
    if [ ! -f /workspace/notebooks/token_diet.py ]; then
      cp /app/notebook.py /workspace/notebooks/token_diet.py
    fi
    exec marimo --quiet edit /workspace/notebooks --host 0.0.0.0 --port "${PORT:-5000}" --headless \
      --no-token
    ;;
  publisher) exec python /app/snapshots.py ;;
  tracking)
    exec mlflow server --host 0.0.0.0 --port "${PORT:-5000}" --workers 1 \
      --backend-store-uri sqlite:////workspace/tracking.db \
      --artifacts-destination /workspace/artifacts --serve-artifacts \
      --allowed-hosts 'dokku.tailf59220.ts.net:8846,127.0.0.1:18401,localhost:18401,localhost:5000,token-experiments.web.1:5000' \
      --cors-allowed-origins 'https://dokku.tailf59220.ts.net:8846'
    ;;
  *) exec "$@" ;;
esac
