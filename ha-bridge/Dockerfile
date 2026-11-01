FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge.py .
# No root, nothing written at run time: config.yaml comes in read-only, the token from the environment.
USER 65534:65534
EXPOSE 8124
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8124/health', timeout=4)"
CMD ["python", "-u", "bridge.py"]
