# CPU-only contract/lifecycle tests. No ComfyUI or model weights required.
FROM python:3.12-slim
WORKDIR /worker
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt pytest jsonschema
COPY handler.py ./
COPY src/ ./src/
COPY workflow/ ./workflow/
COPY tests/ ./tests/
CMD ["python", "-m", "pytest", "tests", "-q"]
