# switchback as a service: one `docker run` gives you the whole cascade
# behind an HTTP endpoint.
#
#   docker build -t switchback .
#   docker run -p 8799:8799 \
#     -e SCRAPER_DISABLE_FIRECRAWL=1 \      # or pass FIRECRAWL_API_KEY to enable it
#     switchback
#   curl localhost:8799/healthz
#
# Playwright's image ships the system libs the stealth browsers need.
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app
COPY . .

# Server + browser tier by default; add ',camoufox,firecrawl,tracing' for the rest.
RUN pip install --no-cache-dir -e ".[server,browser,tracing]" \
    && patchright install chromium

EXPOSE 8799
CMD ["switchback-server"]
