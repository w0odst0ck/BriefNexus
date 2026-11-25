# BriefNexus

Turn any URL into structured data. Self-hosted HTTP collection service.

```bash
docker compose up -d
curl -X POST localhost:9000/v1/url -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/article"}'
```

That's it. No config, no API key, no setup.

---

## What it does

| Endpoint | Purpose |
|---|---|
| `POST /v1/url` | **Give a URL, get title + text + links + images** |
| `GET /v1/quick?source=cninfo` | One-line fetch from a built-in source |
| `POST /v1/collect` | Async task (for long jobs) |
| `GET /v1/sources` | List available sources |

## Response

```json
{
  "status": "ok",
  "title": "Article Title",
  "method": "static",
  "quality": "good",
  "text": "Full article text...",
  "text_chars": 901,
  "links": [{"text": "...", "href": "..."}],
  "images": [{"src": "...", "alt": "..."}]
}
```

`quality`: `good` = full content · `thin` = only page shell (JS site, no browser in container)

## Built-in sources

- `url` — any URL
- `cninfo` — 巨潮资讯 (China listed company announcements)
- `eastmoney` — 东方财富 (China finance news)

Need more? See [Adding a source](#adding-a-source).

---

## Install

### Docker (recommended)

```bash
git clone https://github.com/w0odst0ck/BriefNexus.git
cd BriefNexus
docker compose up -d
```

### Bare Python

```bash
pip install -r platform/requirements.txt -r intel/requirements.txt
python platform/run.py
```

Requires Python 3.12+.

---

## API

### `POST /v1/url`

```json
{"url": "https://...", "force_render": false, "max_chars": 50000}
```

`force_render: true` skips static fetch and goes straight to browser rendering (needs playwright installed locally).

### `GET /v1/quick?source=NAME&params={"k":"v"}`

Blocks until done (max 30s). For longer jobs use `/v1/collect`.

### `POST /v1/collect`

```json
{"source": "eastmoney", "params": {"max_age": 7}}
```

Returns `{"task_id": "t_..."}`. Then:

```bash
curl localhost:9000/v1/tasks/t_...          # status
curl localhost:9000/v1/tasks/t_.../items    # results
```

---

## Adding a source

Drop a file in `intel/collectors/`:

```python
from intel.core.base import BaseCollector, NewsItem

class MyCollector(BaseCollector):
    source_name = "my_source"
    display_name = "My Source"

    def crawl(self, sess) -> list[NewsItem]:
        r = sess.get("https://example.com/feed")
        return [NewsItem(title="...", url="...")]
```

Register in `platform/platform_config.yaml`:

```yaml
sources:
  my_source:
    module: intel.collectors.my_file:MyCollector
    enabled: true
```

Restart. Done — no platform code changes.

---

## Limitations

| Limitation | Why |
|---|---|
| **JS-heavy sites return `thin`** in Docker | Image has no browser (275MB vs 1.5GB+). Install playwright locally, or set `force_render: true` |
| **Single process only** | Storage is in-memory. `--workers>1` breaks task routing |
| **Results expire in 1 hour** | Zero-persistence by design. Nothing written to disk |

---

## Development

```bash
python -m pytest platform/tests intel/tests -q    # run from outside repo root
```

Tests must run outside the repo: the local `platform/` package shadows Python's stdlib `platform` module.

---

## License

MIT