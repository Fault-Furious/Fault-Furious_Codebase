FROM python:3.12-slim
WORKDIR /srv
COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# docker and compose, only used by the fault buttons
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:27-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY agent/mock.py dashboard/server.py dashboard/index.html ./
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
