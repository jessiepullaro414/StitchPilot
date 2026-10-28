# StitchPilot web app.
#   docker build -t stitchpilot-web .
#   docker run -p 8080:8080 stitchpilot-web          -> http://localhost:8080
# Put it behind an HTTPS reverse proxy (or a host that adds HTTPS) before sharing the address:
# AI requests carry the visitor's API key.
FROM python:3.14-slim

# Qt (used offscreen for lettering and PDFs) needs a few system libraries; fonts for lettering
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libegl1 libglib2.0-0 libfontconfig1 libfreetype6 libxkbcommon0 libdbus-1-3 \
        fonts-dejavu fonts-liberation2 fonts-noto-core fonts-open-sans fonts-roboto \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt
COPY stitchpilot ./stitchpilot

RUN useradd --create-home stitch
USER stitch
ENV QT_QPA_PLATFORM=offscreen \
    STITCHPILOT_HOST=0.0.0.0 \
    STITCHPILOT_PORT=8080 \
    STITCHPILOT_TRUST_PROXY=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)"
CMD ["python", "-m", "stitchpilot.web"]
