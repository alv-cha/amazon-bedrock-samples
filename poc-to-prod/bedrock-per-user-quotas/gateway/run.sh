#!/bin/bash
# Entrypoint used by the AWS Lambda Web Adapter (and for local dev).
# The adapter forwards buffered Lambda Function URL requests to this
# uvicorn server.
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}"
