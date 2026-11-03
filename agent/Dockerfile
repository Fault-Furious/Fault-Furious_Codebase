FROM python:3.12-slim
WORKDIR /srv
COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY belief.py .
COPY agent/node.py agent/mock.py agent/shard.py ./
CMD ["uvicorn", "node:app", "--host", "0.0.0.0", "--port", "8000"]
