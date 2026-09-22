FROM python:3.12-slim
RUN pip install --no-cache-dir "fastapi>=0.115,<1" "uvicorn>=0.30,<1"
COPY server.py /app/server.py
WORKDIR /app
USER 65534
EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
