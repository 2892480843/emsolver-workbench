FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp \
    EMSOLVER_DATA_DIR=/data/instance EMSOLVER_WEB_DIST=/app/web/dist EMSOLVER_RESOURCE_DIR=/app
COPY pyproject.toml LICENSE NOTICE.md README.md requirements.lock requirements-cluster.lock ./
COPY emsolver/ ./emsolver/
RUN pip install --no-cache-dir -c requirements-cluster.lock '.[server,worker]'
COPY web/dist/ ./web/dist/
COPY docs/feature-coverage.json docs/dependency-inventory.json ./docs/
COPY docs/third-party-licenses/ ./docs/third-party-licenses/
COPY examples/ ./examples/
COPY benchmarks/ ./benchmarks/
ENTRYPOINT ["python", "-m", "emsolver"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
