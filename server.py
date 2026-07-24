"""ao3-mcp — AO3 (Archive of Our Own) MCP server.

Tools:
  search_works  — search AO3 (20 results/page, up to 5 pages per call, pageable)
  find_tags     — resolve fuzzy wording to canonical AO3 tag names
  get_work      — full metadata card for one work
  read_works    — delegate reading 1-20 fics to the Gemini mini reader
  get_work_text — (escape hatch) raw fic text, bypassing the reader; discouraged

Config comes from the MCP launch params — pass --api-key (and optionally --model,
--backup-model, --min-interval) in the server's "args", or set the matching env vars
(GEMINI_API_KEY, GEMINI_MODEL, GEMINI_MODEL_BACKUP, AO3_MIN_INTERVAL). No .env needed.

It's a small, single-purpose server — feel free to edit anything: the prompts, the
reader model, the throttle, whatever fits your setup.
"""

import argparse
import asyncio
import json
import os
import re
import sys

from mcp.server.fastmcp import FastMCP

from ao3 import AO3Client, MAX_PAGES
from reader import MAX_BATCH_CHARS, MAX_FIC_CHARS, FicReader


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ao3-mcp", description="AO3 (Archive of Our Own) MCP server."
    )
    p.add_argument(
        "--api-key", default=os.environ.get("GEMINI_API_KEY"),
        help="Gemini API key. Free one at https://aistudio.google.com/api-keys. "
             "Falls back to the GEMINI_API_KEY env var.",
    )
    p.add_argument(
        "--model", default=os.environ.get("GEMINI_MODEL", "gemini-flash-latest"),
        help="Model the reader uses (default: gemini-flash-latest).",
    )
    p.add_argument(
        "--backup-model",
        default=os.environ.get("GEMINI_MODEL_BACKUP", "gemini-flash-lite-latest"),
        help="Fallback model when the main one is throttled "
             "(default: gemini-flash-lite-latest).",
    )
    p.add_argument(
        "--min-interval", type=float,
        default=float(os.environ.get("AO3_MIN_INTERVAL", "0.6")),
        help="Minimum seconds between AO3 requests — a politeness throttle "
             "(default: 0.6).",
    )
    args, _ = p.parse_known_args()
    return args


SERVER_INSTRUCTIONS = """\
This server lets you browse the Archive of Our Own (AO3) and — crucially — delegates
the actual READING of fics to a separate model via `read_works`, so a whole novel never
enters your context. Search wide, read the shortlist, recommend only what was read.

Talk like a librarian who has actually read the stacks, not a search engine printing
rows. When you hand fics back to the user:
- Lead with ONE clear pick and WHY it's the one, before any list.
- Sell the reading experience — the voice, the mood, what makes a scene land, who it's
  for — not just word count and kudos. Metadata supports the pitch; it isn't the pitch.
- Have taste. Say what's strongest, name the weak spots honestly, rank when they're
  choosing between options. A good librarian steers, they don't just retrieve.
- Keep it warm and personal, a few well-chosen fics over an exhaustive dump. If the
  reader gave a mood ("something to cry to"), answer the mood.

Two mechanical habits that save you grief:
- Wildcards are your friend — abuse them. `*Genshin Impact*` matches the canonical
  "原神 | Genshin Impact (Video Game)" that an exact fandom filter misses entirely.
- If a fic comes back "(mini reader returned no text …)", the reader refused it —
  usually explicit/extreme content. It's intermittent: retry, or read that fic alone.
"""

mcp = FastMCP("ao3", instructions=SERVER_INSTRUCTIONS)

# Built in main() from the launch params, so importing this module (e.g. for the
# console-script entry point) never requires an API key. The tools below read these
# globals at call time, by which point main() has assigned them.
ao3: AO3Client | None = None
reader: FicReader | None = None

MAX_WORKS_PER_READ = 20


MAX_SUMMARY_CHARS = 350


def _fmt_blurb(w: dict) -> str:
    tags = ", ".join(w["tags"]) if w["tags"] else "—"
    ships = ", ".join(w["relationships"][:4]) if w["relationships"] else "—"
    status = "complete" if w["complete"] else f"WIP ({w['chapters']})"
    summary = w["summary"] or "—"
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS].rsplit(" ", 1)[0] + " […]"
    kh = f", {100 * w['kudos'] / w['hits']:.1f}% k/h" if w.get("hits") else ""
    return (
        f"### {w['title']} — {', '.join(w['authors'])}  (id: {w['work_id']})\n"
        f"{', '.join(w['fandoms'])} | {w['rating']} | {w.get('category') or '?'} | {w['words']:,} words | "
        f"{status} | {w['kudos']:,} kudos{kh} | updated {w['updated']}\n"
        f"Ships: {ships}\nTags: {tags}\n"
        f"Summary: {summary}\n"
    )


@mcp.tool()
async def search_works(
    query: str = "",
    title: str = "",
    author: str = "",
    fandom: str = "",
    relationship: str = "",
    character: str = "",
    tags: str = "",
    rating: str = "",
    categories: str = "",
    complete_only: bool = False,
    word_count: str = "",
    sort_by: str = "relevance",
    page: int = 1,
    pages: int = 1,
) -> str:
    """Search AO3 for works. All filters optional; combine freely.

    RECOMMENDATION WORKFLOW — reading before recommending is MANDATORY, and the
    reading is done by a SEPARATE model, not you. Blurbs are author-written ads;
    never recommend, rank, or summarize a fic from its blurb alone. Cast a wide
    net (pages=2-3, i.e. 40-60 blurbs), shortlist the promising ones, then hand
    the top ≤20 ids to `read_works` — a second AI reads them and reports back.
    Recommend ONLY fics that came back from `read_works`. Do not read fic text
    yourself; delegating it is the entire point of this server.

    SEARCH STRATEGY — searching is cheap and reading is delegated, so the
    winning move is always to OVER-FETCH and let `read_works` brute-force the
    shortlist, never to craft one perfect narrow query. Filters multiply: each
    one you add cuts the pool, and stacked filters routinely cut it to zero.

    USE WILDCARDS LIBERALLY — abuse them. A `*` matches any run of characters
    and works in EVERY name field (`fandom`, `relationship`, `character`, `tags`)
    and in `query`. Wrapping a term in stars is the single best defence against
    AO3's exact-canonical-name trap: `fandom="Genshin Impact (Video Game)"`
    returns ZERO (the canonical tag is actually "原神 | Genshin Impact (Video
    Game)"), but `fandom="*Genshin Impact*"` returns the whole fandom. Likewise
    `relationship="*Kazuha*Scaramouche*"`, `tags="*Enemies to Lovers*"`. When you
    don't know the exact canonical name — which is most of the time — reach for a
    wildcard first instead of guessing the literal string.

    IF YOU GET 0 (or few) RESULTS, that is almost always your query being too
    narrow, NOT the content missing from AO3. Recover instead of giving up:
    - FIRST, wildcard the name fields (`*Genshin Impact*`). This fixes the most
      common cause — an exact-match field that didn't match the canonical tag —
      in one retry, without a separate `find_tags` round-trip.
    - Still unsure of a name? `find_tags` resolves it, or move the idea into
      `query` as free text (fuzzy, no canonical spelling needed).
    - Drop filters one at a time and retry: `word_count` first, then
      `complete_only`, then `rating`. Re-add only what the user insisted on.
    - Concepts don't need to be tags at all: "slow burn rivals in a bakery"
      works fine as free-text `query` even if no such tag exists.
    - Still thin? Search the broad version (fandom + category, sort by kudos),
      fetch 2-3 pages, and let the blurbs + `read_works` do the filtering.
    A human reader has to search narrowly because they can only read a few
    fics; you can read twenty at once, so breadth costs you nothing.

    Results show numeric work ids, not URLs. When relaying a work to the
    user, build the link yourself: https://archiveofourown.org/works/{id}

    Each result shows a kudos-to-hits ratio (k/h) — AO3's most honest quality
    proxy, since kudos are one-per-reader but hits count every visit. Compare
    it only within similar works: multi-chapter fics accumulate hits on every
    chapter visit, so long WIPs run structurally lower ratios than one-shots.

    Args:
        query: free-text search. Supports AO3's full operator syntax
            (case-sensitive, space after colon required where shown):
            `"exact phrase"`, `AND` / `OR` / `NOT`, `-term` to exclude;
            `words>10000`, `words:1000-5000`, `kudos>500` (same for hits/
            comments/bookmarks); `sort:kudos`, `sort:hits`, `sort:>posted`
            (oldest first); `otp: true` (exactly one ship, no side pairings);
            `creators: username` / `-creators: username`; `summary: "phrase"`;
            `expected_number_of_chapters: 1` (one-shots only);
            `series.title: *` (part of a series); `language_id: en`.
            Also supports `*` wildcards, e.g. `*coffee shop*`.
            ⚠️ query is a FULL-TEXT match on the fic body, AND'd with every
            other filter — so it narrows HARD. Do NOT stuff mood/concept
            synonyms here ("nuzzle OR forehead kiss OR won't let go"): that
            demands the prose literally contain one of those strings on top of
            your tag/fandom filters, and routinely collapses a healthy 60-result
            search to 0. Concepts belong in `tags` (wildcarded), not here. Use
            query for author names, quoted title/summary phrases, or the numeric
            operators above — leave it EMPTY when a tag already covers the vibe.
        title: words in the work title.
        author: author/creator name.
        fandom: fandom name, e.g. "Naruto" (comma-separate several). Exact
            canonical match — but `*` wildcards work here: prefer
            "*Genshin Impact*" over the literal name to survive canonical tags
            with prefixes/aliases (e.g. "原神 | Genshin Impact (Video Game)").
        relationship: ship tag. Format: "A/B" romantic, "A & B" platonic,
            canonical name order, e.g. "Kakashi Hatake/Iruka Umino". Wildcards
            work: "*Kazuha*Scaramouche*" beats guessing the exact tag order.
        character: character name(s), comma-separated. Wildcards work here too.
        tags: freeform tags, comma-separated, EXACT canonical spelling
            (use find_tags to resolve, or wildcard it: "*Enemies to Lovers*"). Popular canonical tags: Fluff; Angst;
            Hurt/Comfort; Emotional Hurt/Comfort; Angst with a Happy Ending;
            Hurt No Comfort; Enemies to Lovers; Friends to Lovers; Enemies to
            Friends to Lovers; Slow Burn; Mutual Pining; Fake/Pretend
            Relationship; There Was Only One Bed; Idiots in Love; Getting
            Together; Established Relationship; First Kiss; Found Family;
            Fix-It; Time Travel; Kid Fic; Domestic Fluff; Tooth-Rotting Fluff;
            Crack; Crack Treated Seriously; 5+1 Things; POV Outsider; Soulmates;
            Smut; Plot What Plot/Porn Without Plot; Alpha/Beta/Omega Dynamics;
            Dead Dove: Do Not Eat; Canon Compliant; Post-Canon; Alternate
            Universe - Modern Setting; Alternate Universe - Canon Divergence;
            Alternate Universe - Coffee Shops & Cafés; Alternate Universe -
            College/University; Alternate Universe - Soulmates.
        rating: one of: general, teen, mature, explicit, not rated.
        categories: comma-separated relationship categories to include:
            F/F, F/M, Gen, M/M, Multi, Other. Empty = all.
        complete_only: only finished works.
        word_count: range like "10000-50000", ">5000" or "<20000".
        sort_by: relevance | kudos | hits | comments | bookmarks | words | date_updated | date_posted.
        page: which result page to start from (for paging through results).
        pages: result pages to fetch, 20 works each (1-5). For a targeted
            lookup 1 is enough; for a recommendation hunt fetch 2-3 pages
            (40-60 blurbs) so the read_works shortlist has real competition.
    """
    pages = max(1, min(pages, MAX_PAGES))
    res = await ao3.search(
        page=max(1, page),
        query=query or None,
        title=title or None,
        author=author or None,
        fandom=fandom or None,
        relationship=relationship or None,
        character=character or None,
        tags=tags or None,
        rating=rating or None,
        categories=categories or None,
        complete_only=complete_only,
        word_count=word_count or None,
        sort_by=sort_by,
        pages=pages,
    )
    if not res["works"]:
        return (
            "No works found — the query was too narrow, not AO3 lacking content. "
            "Recover: (1) WILDCARD the name fields — e.g. fandom='*Genshin Impact*' "
            "matches the canonical '原神 | Genshin Impact (Video Game)' that an exact "
            "string misses; `*` works in fandom/relationship/character/tags/query; "
            "(2) still unsure of a name? resolve it with find_tags or move it into "
            "free-text `query`; (3) drop filters one at a time (word_count, "
            "complete_only, rating) and retry; (4) worst case, search just fandom + "
            "category sorted by kudos and let blurbs + read_works do the filtering."
        )
    header = (
        f"{res['total_found']:,} works found on AO3, showing {res['returned']} "
        f"from page {res['start_page']} (sorted by {sort_by}).\n\n"
        if res["total_found"] is not None
        else f"Showing {res['returned']} works from page {res['start_page']}.\n\n"
    )
    return header + "\n".join(_fmt_blurb(w) for w in res["works"])


@mcp.tool()
async def find_tags(term: str, kind: str = "tag") -> str:
    """Resolve fuzzy wording to canonical AO3 tag names (live autocomplete).

    Use before search_works when unsure of exact spelling — e.g. "coffee shop"
    resolves to "Alternate Universe - Coffee Shops & Cafés".

    Args:
        term: partial/fuzzy tag text, e.g. "enemies to", "coffee", "kakashi".
        kind: what to complete: tag | fandom | relationship | character.
    """
    names = await ao3.autocomplete(term, kind)
    if not names:
        return f"No canonical {kind} matches {term!r} — try a shorter fragment."
    return "\n".join(names[:15])


@mcp.tool()
async def get_work(work_id: str) -> str:
    """Get the full metadata card for one work: tags, stats, summary, series info.

    Args:
        work_id: the numeric AO3 work id (from search results or a URL like
            archiveofourown.org/works/12345).
    """
    meta = await ao3.work_meta(work_id.strip())
    return json.dumps(meta, ensure_ascii=False, indent=2)


@mcp.tool()
async def read_works(work_ids: list[str], query: str) -> str:
    """Have the mini reader (a separate AI) read full fics and report on each.

    Works for a single fic or up to 20 at once. You never receive fic text —
    only structured reader reports, one per work. The reader answers your query
    directly (anything works: "is the ending happy?", "how explicit is it?",
    "which of these should I read first?") plus gives a general digest of plot,
    characters, style, and content notes. When given several fics, it ends with
    a comparison section ranking them against your query.

    This is the ONLY approved way to read a fic. A separate model does the
    reading so a whole novel never touches your context. You MUST send fics
    here before you recommend, rank, summarize, or judge them — search blurbs
    are not enough, and reading raw text yourself defeats the entire point of
    this server. Shortlist from blurbs, read here, then recommend.

    Reading depth: a single-fic call sends the reader up to ~150k words (whole
    novels fit); in a batch each fic is capped at ~100k characters. If a long
    fic's report matters, read it alone. Batches that exceed the token budget
    are split internally, then a final reduce pass still produces ONE global
    comparison across the whole batch.

    Content refusals: the reader is Gemini, which has a non-configurable safety
    filter that occasionally refuses explicit or extreme fics — that fic's report
    comes back as "(mini reader returned no text …)". The server already retries
    once on the backup model, but the block is intermittent, so if a fic you care
    about is refused: read it ALONE (a single fic isn't dragged down by an extreme
    one sharing its batch), or just retry. In a mixed batch, one refused fic does
    not sink the others — their reports still return.

    Args:
        work_ids: 1-20 numeric AO3 work ids (from search results or URLs).
        query: the question to answer about each fic.
    """
    if reader is None:
        return (
            "read_works is unavailable — the server was started without a Gemini API "
            "key. Restart it with --api-key (or set GEMINI_API_KEY); free key at "
            "https://aistudio.google.com/api-keys. (search_works, find_tags and "
            "get_work still work without a key.)"
        )
    work_ids = [w.strip() for w in work_ids[:MAX_WORKS_PER_READ]]
    downloads = await asyncio.gather(
        *(ao3.download_text(wid) for wid in work_ids), return_exceptions=True
    )
    failures = []
    works = []
    for wid, fic in zip(work_ids, downloads):
        if isinstance(fic, BaseException):
            failures.append(
                f"## work {wid}: DOWNLOAD FAILED — {type(fic).__name__}: {fic}"
            )
        else:
            meta = {
                "work_id": wid,
                "title": fic["title"],
                "url": f"https://archiveofourown.org/works/{wid}",
                **(fic.get("meta") or {}),
            }
            works.append((meta, fic))

    # Everything goes to Gemini in ONE call so it can compare the fics; only
    # split into chunks if the batch would blow the token budget.
    chunks: list[list] = []
    chunk, chunk_chars = [], 0
    for item in works:
        size = min(item[1]["char_count"], MAX_FIC_CHARS)
        if chunk and chunk_chars + size > MAX_BATCH_CHARS:
            chunks.append(chunk)
            chunk, chunk_chars = [], 0
        chunk.append(item)
        chunk_chars += size
    if chunk:
        chunks.append(chunk)

    split = len(chunks) > 1
    parts = []
    for c in chunks:
        try:
            parts.append(await reader.digest_many(c, query, with_comparison=not split))
        except Exception as exc:
            ids = ", ".join(m["work_id"] for m, _ in c)
            parts.append(f"## works {ids}: READER FAILED — {type(exc).__name__}: {exc}")
    if split:
        # Reduce pass: the per-fic reports are tiny compared to fic text, so one
        # extra small call restores the single global comparison a split batch
        # would otherwise lose.
        try:
            parts.append(await reader.compare_reports(parts, query))
        except Exception as exc:
            parts.append(
                f"(global comparison failed — {type(exc).__name__}: {exc}; "
                f"the {len(chunks)} chunks above stand alone)"
            )
    return "\n\n---\n\n".join(parts + failures)


@mcp.tool()
async def get_work_text(work_id: str, max_words: int = 0) -> str:
    """⚠️ NOT RECOMMENDED — escape hatch only. Returns the raw full text of ONE
    fic directly to you, bypassing the mini reader.

    Prefer `read_works` in almost every case. A fic can run 150k+ words; pulling
    that into your own context buries everything else, burns your tokens, and
    throws away the whole reason this server exists — delegating reading to a
    cheap second model. `read_works` hands you a structured report plus verbatim
    prose samples, which is enough to judge, compare, and recommend a fic without
    the fic ever entering your context.

    Only reach for this when you genuinely need exact wording a report can't carry
    — e.g. the user explicitly asks you to quote or close-read a specific passage.
    If you just want to know what a fic is like or whether it's good: use
    `read_works` instead.

    Args:
        work_id: the numeric AO3 work id.
        max_words: cap the text to the first N words (0 = whole fic). Set a limit
            to sample a fic's opening instead of dumping the entire thing into
            your context — a few thousand words is usually plenty to judge voice.
    """
    fic = await ao3.download_text(work_id.strip())
    text = fic["text"]
    truncated = False
    if max_words and max_words > 0:
        count, end = 0, len(text)
        for m in re.finditer(r"\S+", text):
            count += 1
            if count == max_words:
                end = m.end()
                break
        if end < len(text):
            text, truncated = text[:end], True
    header = f"# {fic['title']}"
    if fic.get("byline"):
        header += f" — {fic['byline']}"
    body = f"{header}\n\n{text}"
    if truncated:
        body += f"\n\n[… truncated to the first {max_words:,} words]"
    return body


def main() -> None:
    """Console entry point (``ao3-mcp``) and ``python server.py`` launcher."""
    global ao3, reader
    args = _parse_args()
    ao3 = AO3Client(min_interval=args.min_interval)
    # Only read_works needs the reader (and its key). Boot without it when no key
    # is given, so the server still starts and exposes its tools for introspection
    # — read_works reports the missing key when it's actually called.
    if args.api_key:
        reader = FicReader(
            api_key=args.api_key, model=args.model, backup_model=args.backup_model
        )
    reader_status = (
        f"{reader.model} (backup {reader.backup_model})" if reader
        else "disabled — no API key; read_works will report this"
    )
    # Tiny startup note (goes to the client's MCP log, not to tool output).
    print(
        f"ao3-mcp · reader: {reader_status} · "
        f"throttling AO3 to one request every {args.min_interval:g}s, to be polite",
        file=sys.stderr,
    )
    mcp.run()


if __name__ == "__main__":
    main()
