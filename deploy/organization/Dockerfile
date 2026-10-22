FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir 'pydantic>=2.7,<3' 'jsonschema>=4.23' 'packaging>=24.0' 'httpx>=0.27,<1' 'PyYAML>=6.0' 'starlette>=1.3.1,<2' 'uvicorn>=0.30,<1' 'regex>=2024.5.15' 'psycopg[binary,pool]>=3.2,<4'
COPY src/suiteharness /app/src/suiteharness
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
# Source deployment. The framework itself is loaded directly from /app/src.
