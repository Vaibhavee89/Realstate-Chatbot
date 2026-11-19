FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PORT=8000 HF_HOME=/srv/hf
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY data ./data
# precompute chunk embeddings and cache the small embedding model inside the image (no network needed at runtime)
RUN python -m app.build_index && rm -rf /root/.cache
ENV HF_HUB_OFFLINE=1
RUN useradd -m app && chown -R app /srv
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://localhost:'+os.environ.get('PORT','8000')+'/health')"
CMD ["sh","-c","uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
