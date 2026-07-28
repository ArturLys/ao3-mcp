# ao3-mcp

<!-- mcp-name: io.github.ArturLys/ao3-mcp -->

[![PyPI](https://img.shields.io/pypi/v/ao3-mcp)](https://pypi.org/project/ao3-mcp/)
[![CI](https://github.com/ArturLys/ao3-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ArturLys/ao3-mcp/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)

[![ao3-mcp MCP server](https://glama.ai/mcp/servers/ArturLys/ao3-mcp/badges/score.svg)](https://glama.ai/mcp/servers/ArturLys/ao3-mcp)

An [MCP](https://modelcontextprotocol.io/) (Model Context Protocol) server that connects AI agents — Claude, Cursor, or any MCP client — to the [Archive of Our Own](https://archiveofourown.org/). Search AO3 fanfiction with full filters, resolve fuzzy wording to canonical tags, and get fics *actually read* before they're recommended.

The trick: your agent never reads fic text. It delegates reading to a cheap secondary model (Gemini), which digests whole fics — even 150k-word novels — and returns structured reports. Your agent's context stays clean; the recommendations are based on the real text, not the blurb.

```
agent ──MCP──> server.py
                 ├─ ao3.py     AO3 scraping (no public API exists) — throttled and polite
                 └─ reader.py  Gemini reads the fics, reports back: plot, style,
                               prose samples, content notes, a ranking
```

## Why this beats blurb-based recommendations

An AO3 blurb is an ad written by the author. This server's workflow is: search wide (40–60 results), have the reader model read the shortlist — up to 20 full fics in one call — and recommend only what was actually read, with verbatim prose samples so quality is judged from the text itself.

## Not just for finding your next read

If you write with an AI — fanfic, original fiction, roleplay — this doubles as an inspiration engine. Mid-scene, your agent can pull up how real fic authors handle the exact beat you're on:

```
Find three highly-kudosed fics where rivals are forced to share a bed, read them,
and tell me how each one builds the tension — pacing, POV, what they leave unsaid.
```

The reader reports back with structure, style notes, and verbatim prose samples, so the model gets grounded in how the trope is actually written — not what it imagines fanfic sounds like. Works the same for roleplay: pull reports on fics that nail a character's voice and feed them in as style reference.

## Install

Requires **Python 3.10+** and a free **Gemini API key**:

> Go to [aistudio.google.com/api-keys](https://aistudio.google.com/api-keys), sign in with any Google account, and click **"Create API key"**. The free tier is enough — no billing setup needed.

```bash
pip install ao3-mcp
```

## Add to your agent

Point `command` at `ao3-mcp` and pass your key with `--api-key`:

```json
{
  "mcpServers": {
    "ao3": {
      "command": "ao3-mcp",
      "args": ["--api-key", "YOUR_GEMINI_KEY"]
    }
  }
}
```

Prefer to keep the key out of the args list? Drop `--api-key` and pass it in an `env`
block instead — the server reads `GEMINI_API_KEY` from the environment as a fallback:

```json
"env": { "GEMINI_API_KEY": "YOUR_GEMINI_KEY" }
```

<details>
<summary>Claude Code</summary>

```bash
claude mcp add ao3 -- ao3-mcp --api-key YOUR_GEMINI_KEY
```

</details>

<details>
<summary>Cursor</summary>

`Cursor Settings` → `MCP` → `New MCP Server`, paste the JSON config above.

</details>

<details>
<summary>Google Antigravity</summary>

Add the JSON config above to `.gemini/antigravity/mcp_config.json`.

</details>

<details>
<summary>VS Code / Copilot</summary>

```bash
code --add-mcp '{"name":"ao3","command":"ao3-mcp","args":["--api-key","YOUR_GEMINI_KEY"]}'
```

</details>

Then just ask:

```
Find me a completed enemies-to-lovers longfic in <fandom>, read the top candidates, and tell me which is best written.
```

### Launch params

| Param             | Env var               | Default              | What it does                                  |
| ----------------- | --------------------- | -------------------- | --------------------------------------------- |
| `--api-key`       | `GEMINI_API_KEY`      | —                    | Gemini API key (required).                     |
| `--model`         | `GEMINI_MODEL`        | `gemini-flash-latest`| Model the reader uses.                          |
| `--backup-model`  | `GEMINI_MODEL_BACKUP` | `gemini-flash-lite-latest` | Fallback model when the main one is throttled. |
| `--min-interval`  | `AO3_MIN_INTERVAL`    | `0.6`                | Minimum seconds between AO3 requests.           |

## Tools

| Tool           | What it does                                                                 |
| -------------- | ---------------------------------------------------------------------------- |
| `search_works` | Search AO3: fandom, ship, character, tags, rating, word count, completion, sorting. 20 results/page, up to 5 pages per call. The `query` field supports AO3's full search-operator syntax (`words>10000`, `kudos>500`, `sort:kudos`, …). |
| `find_tags`    | Live autocomplete — fuzzy wording → canonical AO3 tag, fandom, ship, or character names. |
| `get_work`     | Full metadata card for one work: tags, stats, summary, series info.          |
| `read_works`   | Reads 1–20 full fics with the secondary model and returns a structured report per fic — plot, characters, style, verbatim prose samples, content notes — plus a comparison ranking them against your question. |

Fic downloads are cached locally for 24h, so re-reading a fic with a new question costs no AO3 requests.

## Good to know

- **AO3 has no API** — this scrapes its (clean) HTML, one request at a time, throttled to one every **0.6s** by default (tune with `--min-interval`) and honoring `Retry-After`. AO3 is volunteer-run; the politeness is deliberate.
- **Cloudflare:** AO3 blocks plain HTTP clients. This uses `curl_cffi` with a mobile-Safari TLS fingerprint, which passes as of writing. If requests start failing with 403 + `cf-mitigated: challenge`, change `IMPERSONATE` in `ao3.py`.
- **Privacy:** fic text goes to Google's Gemini API for reading; nothing else leaves your machine, no telemetry.
- **Adult content:** AO3 hosts works across all ratings. The server passes through whatever your search scopes — use the `rating` filter and AO3's warning tags to control what gets fetched.

## Make it yours

It's a small, single-purpose server — a few hundred readable lines with no framework magic. Fork it and edit anything: rewrite the reader's prompt, swap in a different model, change the throttle, add a tool. That's the intended way to use it.

Run it from source:

```bash
git clone https://github.com/ArturLys/ao3-mcp.git
cd ao3-mcp
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python smoke_test.py YOUR_GEMINI_KEY   # end-to-end check: search → download → digest
python server.py --api-key YOUR_GEMINI_KEY   # or point your client's command at this
```

## Credits

- AO3 access approach builds on [ao3_api](https://github.com/wendytg/ao3_api) by wendytg.
- All fanworks belong to their authors on the [Archive of Our Own](https://archiveofourown.org/), a project of the [Organization for Transformative Works](https://www.transformativeworks.org/).

## License

MIT
