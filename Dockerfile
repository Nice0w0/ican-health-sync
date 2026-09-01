# Only needed if you self-host instead of deploying to a serverless tier.
# No dependencies: the .xls reader is standard library only.
FROM python:3.12-slim
WORKDIR /app
COPY api ./api
COPY server.py ./
ENV CGM_HOST=0.0.0.0
EXPOSE 8000
CMD ["python3", "server.py"]
