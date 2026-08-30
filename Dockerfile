FROM python:3.11-slim
# Optional custom pip index (e.g. tuna mirror); empty = default PyPI.
ARG PIP_INDEX_URL=""
WORKDIR /app
COPY serpapi_proxy/requirements.txt .
RUN if [ -n "$PIP_INDEX_URL" ]; then \
      pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi
COPY serpapi_proxy/ ./serpapi_proxy/
ENV PORT=8001
CMD ["python", "-m", "serpapi_proxy"]