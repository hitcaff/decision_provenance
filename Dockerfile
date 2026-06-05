FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY decision_provenance/ ./decision_provenance/
COPY setup.py .
COPY pyproject.toml .
COPY README.md .

# Install the package with API extras
RUN pip install --no-cache-dir -e ".[api]"
RUN pip install --no-cache-dir uvicorn fastapi pydantic

# Create directory for the SQLite database
RUN mkdir -p /data

# Expose port
EXPOSE 8000

# Environment variables (override at runtime)
ENV DB_PATH=/data/provenance.db
ENV HOST=0.0.0.0
ENV PORT=8000

# Run the API
CMD ["python", "-m", "uvicorn", "decision_provenance.api:app", "--host", "0.0.0.0", "--port", "8000"]
