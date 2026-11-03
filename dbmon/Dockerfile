FROM python:3.12-slim
WORKDIR /srv
RUN pip install --no-cache-dir "psycopg[binary]==3.2.3"
COPY otel.py dbmon/dbmon.py ./
CMD ["python", "-u", "dbmon.py"]
