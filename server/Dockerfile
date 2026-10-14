FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cyril_server ./cyril_server
EXPOSE 8000
CMD ["uvicorn", "cyril_server.app:app", "--host", "0.0.0.0", "--port", "8000"]
