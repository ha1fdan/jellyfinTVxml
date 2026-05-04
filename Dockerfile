FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/ha1fdan/jellyfinTVxml"

WORKDIR /app

COPY server.py .

# streams.json and channel_ids.json are bind-mounted at runtime (see compose.yml).
# Provide empty defaults so the container starts without them.
RUN echo '{}' > streams.json && echo '{}' > channel_ids.json

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/epg.xml?days=0')" || exit 1

CMD ["python3", "-u", "server.py"]
