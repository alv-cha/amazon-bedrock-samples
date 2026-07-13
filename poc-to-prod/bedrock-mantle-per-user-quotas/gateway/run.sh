#!/bin/bash
# Entrypoint used by the AWS Lambda Web Adapter (and for local dev).
# The adapter turns the Lambda Function URL (RESPONSE_STREAM) into plain
# HTTP against this uvicorn server, which is what enables true SSE
# streaming from a Python Lambda.
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}"
