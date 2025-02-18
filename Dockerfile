FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Add retry mechanism and longer timeout for pip
RUN pip install --upgrade pip && \
    pip install --default-timeout=100 -r requirements.txt --target=/app/local || \
    pip install --default-timeout=100 -r requirements.txt --target=/app/local || \
    pip install --default-timeout=100 -r requirements.txt --target=/app/local

FROM python:3.11-slim

WORKDIR /app

# Create necessary directories first
RUN mkdir -p /app/templates /app/logs && \
    touch /command_executor.log

# Copy application code
COPY web_app.py /app/
COPY script.py /app/
COPY config.yaml /app/
COPY templates/ /app/templates/

# Copy built dependencies
COPY --from=builder /app/local /usr/local

ENV PYTHONPATH="/usr/local:${PYTHONPATH}"
ENV PATH="/usr/local/bin:${PATH}"

# Make sure we're in the correct directory
WORKDIR /app

# Run the web application
CMD ["python", "web_app.py"]