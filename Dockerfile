FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt --target=/app/local

# ---

FROM python:3.11-slim

WORKDIR /app

# Copy application code
COPY . .

# Copy built dependencies
COPY --from=builder /app/local /usr/local

RUN touch /command_executor.log

ENV PYTHONPATH="/usr/local:${PYTHONPATH}"
ENV PATH="/usr/local/bin:${PATH}"

ENTRYPOINT ["python", "script.py"]