"""End-to-end smoke test: search -> pick a short fic -> download -> Gemini digest.

Run it with your key:  python smoke_test.py YOUR_GEMINI_KEY
Or set GEMINI_API_KEY in the environment first and run:  python smoke_test.py
"""

import asyncio
import os
import sys

if len(sys.argv) > 1:
    os.environ["GEMINI_API_KEY"] = sys.argv[1]

from ao3 import AO3Client
from reader import FicReader


async def main():
    ao3 = AO3Client()
    try:
        print("=== 1. search ===")
        res = await ao3.search(
            tags="Alternate Universe - Coffee Shops & Cafés",
            rating="general",
            complete_only=True,
            word_count="1000-5000",
            sort_by="kudos",
            pages=1,
        )
        print(f"total_found={res['total_found']} returned={res['returned']}")
        if not res["works"]:
            print("SEARCH PARSING FAILED — no works parsed")
            sys.exit(1)
        for w in res["works"][:3]:
            print(f"  [{w['work_id']}] {w['title']} — {w['authors']} | "
                  f"{w['words']}w | {w['kudos']} kudos | {w['rating']} | complete={w['complete']}")
            print(f"      fandoms={w['fandoms'][:2]} ships={w['relationships'][:2]}")
            print(f"      summary={w['summary'][:120]!r}")

        pick = res["works"][0]["work_id"]

        print(f"\n=== 2. work_meta({pick}) ===")
        meta = await ao3.work_meta(pick)
        for k in ("title", "authors", "rating", "words", "chapters", "complete", "kudos", "published"):
            print(f"  {k}: {meta[k]}")

        print(f"\n=== 3. download_text({pick}) ===")
        fic = await ao3.download_text(pick)
        print(f"  title={fic['title']!r} byline={fic['byline']!r} chars={fic['char_count']}")
        print(f"  first 200 chars: {fic['text'][:200]!r}")

        print("\n=== 4. gemini digest ===")
        reader = FicReader()
        report = await reader.digest(meta, fic, "General impression — is it good, and is the ending satisfying?")
        print(report)
    finally:
        await ao3.close()


asyncio.run(main())
