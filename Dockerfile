FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY data ./data
RUN useradd -m app && chown -R app /srv
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://localhost:'+os.environ.get('PORT','8000')+'/health')"
CMD ["sh","-c","uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
