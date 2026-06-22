# Calling the engine from Node (or any non-Python app)

Other tools don't re-implement the cascade — they call the engine. For *generic*
page→markdown fetches, shell out to the CLI or (preferred) hit the HTTP service.
Structured job-board providers (greenhouse/lever/ashby APIs) can stay where they
are, or later move into Tier 0 as structured strategies.

## HTTP bridge (preferred)
Run the service once (`switchback-server`, or `docker run -p 8799:8799
switchback`) and have every tool — Python or Node — hit the same endpoint.
One warm process keeps the Tier-3 browser pool hot instead of cold-starting a
subprocess per call.

```js
const base = process.env.SCRAPER_ENGINE_URL ?? "http://localhost:8799";

export async function scrape(urls) {
  const res = await fetch(`${base}/scrape`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ urls: Array.isArray(urls) ? urls : [urls] }),
  });
  if (!res.ok) throw new Error(`engine ${res.status}`);
  return res.json(); // [{ url, source_method, markdown }]
}
```

## CLI bridge (no running service)
```js
import { execFile } from "node:child_process";
import { promisify } from "node:util";
const execFileP = promisify(execFile);

// Point SCRAPER_ENGINE_DIR at your checkout of this repo.
const engineDir = process.env.SCRAPER_ENGINE_DIR ?? process.cwd();

export async function scrape(url) {
  const { stdout } = await execFileP("python3", ["-m", "engine", url], {
    cwd: engineDir,
    maxBuffer: 32 * 1024 * 1024,
  });
  return JSON.parse(stdout); // [{ url, source_method, markdown }]
}
```
