FROM python:3.11-slim
WORKDIR /app
COPY mcp_server.py .
RUN pip install --no-cache-dir fastmcp httpx anthropic
EXPOSE 8443
CMD ["python", "mcp_server.py"]
