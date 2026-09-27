# Apify Actor image: switchback tiers 1–4 (HTTP + anti-bot solver + stealth Chromium).
# Build context is the repo root (see dockerContextDir in actor.json), so the Actor
# always runs the switchback source it ships with.
FROM apify/actor-python-playwright:3.12

COPY . ./

# cloudscraper 3.x fork enables tier_3 stealth (PyPI only has the frozen 1.x/2.x);
# a tarball URL avoids needing git in the image.
RUN pip install --no-cache-dir ".[browser,cloudflare]" "apify~=4.0" \
        "cloudscraper @ https://github.com/VeNoMouS/cloudscraper/archive/refs/tags/3.0.0.tar.gz" \
    && patchright install chromium

CMD ["python3", "-m", "actor.main"]
