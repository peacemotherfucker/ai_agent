FROM python:3.9-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt --target=/app/local

# ---

FROM python:3.9-slim

WORKDIR /app

# Create secure non-root user
RUN addgroup --system appuser && \
    adduser --system --ingroup appuser appuser && \
    mkdir -p /home/appuser/local

# Copy application code
COPY . .

# Set proper permissions on the /app directory
RUN chown -R appuser:appuser /app

# Copy built dependencies to appuser's local directory
COPY --from=builder /app/local /home/appuser/local

RUN touch /home/appuser/command_executor.log && chown appuser:appuser /home/appuser/command_executor.log

USER appuser

ENV PYTHONPATH="/home/appuser/local:${PYTHONPATH}"
ENV PATH="/home/appuser/local/bin:${PATH}"

ENTRYPOINT ["python", "script.py"]
