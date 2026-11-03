FROM python:3.12.16-slim
WORKDIR /app
COPY collector/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY collector/collector.py db/schema.sql ./
ENTRYPOINT ["python", "/app/collector.py"]
CMD ["serve"]
