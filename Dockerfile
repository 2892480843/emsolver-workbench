FROM node:22-bookworm-slim AS frontend
WORKDIR /build/web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY emsolver/ ./emsolver/
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps .
COPY LICENSE NOTICE.md ./
COPY --from=frontend /build/web/dist ./web/dist
COPY examples/ ./examples/
COPY benchmarks/ ./benchmarks/
COPY docs/feature-coverage.json ./docs/feature-coverage.json
COPY docs/third-party-licenses/ ./docs/third-party-licenses/
ENV EMSOLVER_DATA_DIR=/data
ENV EMSOLVER_WEB_DIST=/app/web/dist
ENV EMSOLVER_RESOURCE_DIR=/app
VOLUME /data
EXPOSE 8000
# Set EMSOLVER_API_TOKEN when publishing this port outside localhost.
CMD ["emsolver", "serve", "--host", "0.0.0.0", "--port", "8000"]
