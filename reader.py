"""The mini librarian: delegates actual fic reading to Gemini.

The main AI never sees fic text — it sends a work_id + a question,
and gets back a structured digest written by the mini reader.
Fic text is capped at MAX_FIC_CHARS; the mini reader is told the
original length so it can flag how much of the fic its digest covers.
"""

import os

from google import genai
from google.genai import errors, types

MAX_FIC_CHARS = 100_000
# One batched request must stay comfortably inside free-tier context/TPM limits
# (~200k tokens): 600k chars ≈ 150k tokens, leaving room for prompt + output.
MAX_BATCH_CHARS = 600_000
# A single-fic read gets the whole budget: ~150k words. ≈225k tokens, which
# rides close to the free-tier TPM ceiling — the 429 fallback below catches it.
MAX_SINGLE_FIC_CHARS = 900_000

READER_SYSTEM_PROMPT = """\
You are a fanfiction reader-librarian. You receive the text of one or SEVERAL fanfics
from AO3 (each possibly truncated) plus a query from another AI assistant that has NOT
read them. Your job: read everything and produce one structured report per fic so the
assistant can discuss them with its user without ever reading the fics itself.

Input format: the query, then each fic in a "=== FIC k of n: title (id) ===" block
containing a metadata block, a TRUNCATION line, and the fic text.
If a TRUNCATION line says the text was cut, you only received the beginning of that
fic — never pretend to know how it ends, and state clearly what portion you covered.

Output: one report per fic, in the order given, each formatted as below. If you were
given MORE than one fic, end with a final section:
## Comparison
3-6 sentences directly comparing the fics against the query — rank them when the query
implies a choice ("which should I read?"), highlight the standout and the weakest, and
say what differentiates them. Refer to fics by title, never by number alone.

Per-fic report format (markdown, keep each report under ~500 words):
## <Title> — reader report
- **Link:** the work's url from its metadata (so the user can open the fic)
- **Coverage:** "full fic" or "first N% only (truncated)"
- **Answer to the query:** direct, specific answer to the question asked. This is the
  most important section — quote short passages if they help.
- **Plot:** 3-6 sentence summary of what actually happens (in what you read).
- **Characters & dynamics:** who matters and how they relate; note characterization
  quality (in-character? OOC?).
- **Style & tone:** prose quality, pacing, mood, POV, anything notable.
- **Prose sample:** 2-3 SHORT verbatim quotes (one descriptive passage, one dialogue
  exchange). Pick TYPICAL lines, not the best ones — the assistant judges prose
  quality from these itself, so quote exactly, never paraphrase or cherry-pick.
- **Content notes:** anything a reader might want warning about that the tags may not
  cover (graphic scenes, major character death, heavy themes).
- **Verdict:** one honest sentence — is it good, and who would enjoy it?

Be concrete and opinionated, never vague. If the query asks something the text you
received cannot answer (e.g. about the ending of a truncated fic), say so explicitly.

Content policy: AO3 hosts everything from General to Explicit, and your reader is an
adult who chose the fic knowingly — tags included. Explicit sex, kink, violence, and
dark themes are ordinary archival material here, not something to refuse, warn about
twice, or moralize over. Report on such content the way a literature archivist would:
state plainly what happens, how explicit it is, and how well it's written. Summarize
rather than reproduce — short quotes are fine, transcribing whole scenes is not.
Never respond with a refusal; if a fic is extreme, SAY it's extreme and describe how.
"""

COMPARE_SYSTEM_PROMPT = """\
You are the same fanfiction reader-librarian. You already read a batch of fics in
several separate sittings; below are YOUR OWN per-fic reports from those sittings,
plus the original query. Write ONE section:

## Comparison (all fics)

Compare and rank ALL the fics against the query as if you had read them in one go —
name the standout, the weakest, and what differentiates the contenders. Refer to fics
by title, never by number. Use the Prose sample sections to weigh writing quality.
Do not repeat the reports. Output only this section, under 300 words.
"""


class FicReader:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        backup_model: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "No Gemini API key. Pass --api-key in the MCP server args (or set "
                "GEMINI_API_KEY). Get a free key at https://aistudio.google.com/api-keys."
            )
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
        self.backup_model = backup_model or os.environ.get(
            "GEMINI_MODEL_BACKUP", "gemini-flash-lite-latest"
        )
        self._client = genai.Client(api_key=self.api_key)

    @staticmethod
    def _fic_block(index: int, total: int, meta: dict, fic: dict, cap: int) -> str:
        text = fic["text"]
        original_len = len(text)
        if original_len > cap:
            text = text[:cap]
            pct = round(100 * cap / original_len)
            trunc_line = (
                f"TRUNCATION: text was cut at {cap:,} of {original_len:,} "
                f"characters — you have roughly the first {pct}% of the fic."
            )
        else:
            trunc_line = "TRUNCATION: none — you have the full fic."
        meta_block = "\n".join(
            f"{k}: {v}" for k, v in meta.items() if v not in (None, "", [], {})
        )
        title = meta.get("title") or fic.get("title") or "untitled"
        work_id = meta.get("work_id") or fic.get("work_id") or "?"
        return (
            f"=== FIC {index} of {total}: {title} (id: {work_id}) ===\n"
            f"--- metadata ---\n{meta_block}\n\n{trunc_line}\n\n"
            f"--- text ---\n{text}"
        )

    async def digest(self, meta: dict, fic: dict, query: str) -> str:
        return await self.digest_many([(meta, fic)], query)

    async def digest_many(
        self,
        works: list[tuple[dict, dict]],
        query: str,
        with_comparison: bool = True,
    ) -> str:
        """One Gemini call covering all given fics; ends with a comparison
        section when there is more than one. A single-fic call gets a much
        larger text cap (MAX_SINGLE_FIC_CHARS). Set with_comparison=False for
        one chunk of a split batch — the global comparison is produced
        separately by compare_reports()."""
        total = len(works)
        cap = MAX_SINGLE_FIC_CHARS if total == 1 else MAX_FIC_CHARS
        blocks = [
            self._fic_block(i, total, meta, fic, cap)
            for i, (meta, fic) in enumerate(works, start=1)
        ]
        prompt = (
            f"=== QUERY FROM THE ASSISTANT ===\n{query}\n\n" + "\n\n".join(blocks)
        )
        if total > 1 and not with_comparison:
            prompt += (
                "\n\n=== NOTE ===\nThese fics are one chunk of a larger batch. "
                "Do NOT write the final Comparison section — it is produced "
                "separately over the whole batch."
            )
        return await self._generate(prompt, READER_SYSTEM_PROMPT)

    async def compare_reports(self, reports: list[str], query: str) -> str:
        """Reduce pass: one small Gemini call over the per-fic REPORTS (not the
        fic texts) producing a single global comparison section. This is what
        lets a batch that had to be split into several reader calls still end
        with one ranking across everything."""
        prompt = (
            f"=== ORIGINAL QUERY FROM THE ASSISTANT ===\n{query}\n\n"
            f"=== YOUR REPORTS FROM SEPARATE SITTINGS ===\n\n"
            + "\n\n---\n\n".join(reports)
        )
        return await self._generate(prompt, COMPARE_SYSTEM_PROMPT)

    @staticmethod
    def _no_text_reason(resp) -> str:
        if resp.candidates:
            return f"finish_reason={resp.candidates[0].finish_reason}"
        if resp.prompt_feedback:
            return f"prompt_feedback={resp.prompt_feedback}"
        return "unknown reason"

    async def _generate(self, prompt: str, system_prompt: str) -> str:
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.4,
            safety_settings=[
                types.SafetySetting(category=cat, threshold="BLOCK_NONE")
                for cat in (
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "HARM_CATEGORY_HARASSMENT",
                    "HARM_CATEGORY_HATE_SPEECH",
                    "HARM_CATEGORY_DANGEROUS_CONTENT",
                )
            ],
        )

        async def _call(model: str):
            return await self._client.aio.models.generate_content(
                model=model, contents=prompt, config=config
            )

        # First attempt on the main model.
        try:
            resp = await _call(self.model)
            if resp.text:
                return resp.text
            first_reason = self._no_text_reason(resp)
        except errors.APIError as exc:
            # Only overload/throttle codes are worth a retry; anything else
            # (bad request, auth, missing model) should surface immediately.
            if exc.code not in (429, 500, 503, 504):
                raise
            first_reason = f"APIError {exc.code}"

        # Retry once on the backup model. This catches BOTH transient API errors
        # AND Gemini's PROHIBITED_CONTENT block — that block is non-configurable
        # (BLOCK_NONE above doesn't disable it) and fires probabilistically on
        # explicit/extreme fics, so a second pass on a different model often gets
        # through where the first was refused.
        try:
            resp = await _call(self.backup_model)
        except errors.APIError as exc:
            return (
                f"(mini reader failed — main model: {first_reason}; "
                f"backup model: APIError {exc.code})"
            )
        if resp.text:
            return resp.text
        return (
            f"(mini reader returned no text — main model {first_reason}, backup "
            f"model {self._no_text_reason(resp)}. Likely a content refusal on an "
            f"explicit/extreme fic; it's intermittent, so retrying or reading this "
            f"fic alone may still work.)"
        )
