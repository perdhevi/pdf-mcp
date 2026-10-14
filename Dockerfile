FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-ind \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system --no-cache .

ENV PDF_MCP_DATA_DIR=/data \
    PDF_MCP_HTTP_HOST=0.0.0.0 \
    PDF_MCP_HTTP_PORT=8765 \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8765
CMD ["pdf-mcp", "serve", "--transport", "http"]
