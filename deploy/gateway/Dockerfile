FROM python:3.12.7-slim
WORKDIR /app
COPY requirements-gateway.txt .
RUN pip install --no-cache-dir -r requirements-gateway.txt
COPY src/__init__.py src/rr_gateway.py ./src/
USER 65532:65532
CMD ["python", "-m", "src.rr_gateway"]
