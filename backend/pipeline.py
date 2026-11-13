"""The matching pipeline: screenshot -> pieces -> candidates -> verified matches.

Built for speed: nothing waits for anything it doesn't need.

1. detect_pieces  Claude STREAMS just a label + box per piece, so boxes appear
                  on screen within a couple of seconds.
   describe       In parallel, one small call per piece writes its search text
                  (brand, query). Usually done before the user even taps.
2. crop           Cut the piece out so visual search sees only the blazer.
3. search         Google Lens on the crop + Google Shopping on the text query,
                  both fired immediately, in parallel.
4. verify         Claude grades every candidate photo against the crop
                  (exact / close / similar / unrelated). The UI shows Lens's
                  own ranking first, then swaps in the verified list.
"""

from __future__ import annotations

import asyncio
import base64
import html
import io
import os
import re
from typing import AsyncIterator, Literal
from urllib.parse import urlparse

import anthropic
import httpx
from PIL import Image
from pydantic import BaseModel, Field

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_EDGE = 1280          # enough detail for clothing, far fewer image tokens than full-res
MAX_CANDIDATES = 12      # per piece, sent to verification
THUMB_EDGE = 256

SOCIAL_HOSTS = (
    "instagram.", "pinterest.", "tiktok.", "reddit.", "facebook.", "x.com",
    "twitter.", "youtube.", "tumblr.", "lemon8", "threads.",
    "faire.com", "alibaba.", "1688.", "made-in-china.", "dhgate.", "globalsources.",  # wholesale, not for shoppers
)

_claude: anthropic.AsyncAnthropic | None = None
_http: httpx.AsyncClient | None = None


def claude() -> anthropic.AsyncAnthropic:
    global _claude
    if _claude is None:
        _claude = anthropic.AsyncAnthropic()
    return _claude


def http() -> httpx.AsyncClient:
    """One shared client so TLS connections to SerpAPI and image CDNs stay warm."""
    global _http
    if _http is None:
        _http = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (seen)"},
            limits=httpx.Limits(max_connections=60, max_keepalive_connections=30),
            http2=False,
        )
    return _http


def fast_kwargs() -> dict:
    # Fast mode: same model, up to ~2.5x faster output, 2x the token price. FAST_MODE=0 to turn off.
    return {"speed": "fast"} if os.getenv("FAST_MODE", "1") == "1" else {}


# ---------------------------------------------------------------- schemas
# Two steps so boxes appear fast: Spot is all the UI needs to draw a box (tiny,
# streams in ~1-2s); Details (search text) is written per piece in parallel, after.

class Spot(BaseModel):
    label: str = Field(description="1-2 word name, e.g. 'Blazer', 'Knit top', 'Bag', 'Socks'")
    color_hex: str = Field(description="Dominant color as #RRGGBB")
    box: list[int] = Field(description="[x0, y0, x1, y1] pixel bounding box of the piece in the image")
    category: Literal[
        "top", "outerwear", "bottom", "dress", "shoes", "socks", "bag", "accessory", "jewelry", "eyewear", "hat"
    ]


class Details(BaseModel):
    brand_guess: str | None = Field(description="Brand only if visible (logo, tag, signature design) or unmistakable; else null")
    search_query: str = Field(
        description="Google Shopping query that would find this exact piece. If the brand is known, use the brand "
        "and the product's common name (e.g. 'Skims Soft Lounge long slip dress black'); otherwise describe it "
        "(e.g. 'women's camel wool oversized single-breasted blazer')."
    )
    broad_query: str = Field(description="Shorter, more generic shopping query (3-6 words) used if the specific one finds little")
    description: str = Field(description="One sentence: color, material, cut, distinctive details.")


class Grade(BaseModel):
    index: int
    match: Literal["exact", "close", "similar", "unrelated"]
    score: int = Field(description="0-100 visual similarity to the target piece")
    is_product: bool = Field(description="True if this looks like a buyable product listing")


class Grades(BaseModel):
    grades: list[Grade]


# ---------------------------------------------------------------- helpers

def prepare_image(raw: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    img.thumbnail((MAX_EDGE, MAX_EDGE))
    return img


def to_jpeg(img: Image.Image, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def image_block(jpeg: bytes) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(jpeg).decode()},
    }


def crop_piece(img: Image.Image, box: list[int], pad: float = 0.06) -> Image.Image:
    x0, y0, x1, y1 = box
    w, h = img.size
    px, py = (x1 - x0) * pad, (y1 - y0) * pad
    x0, y0 = max(0, int(x0 - px)), max(0, int(y0 - py))
    x1, y1 = min(w, int(x1 + px)), min(h, int(y1 + py))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return img
    return img.crop((x0, y0, x1, y1))


def clamp_box(box: list[int], w: int, h: int) -> list[int]:
    x0, y0, x1, y1 = (list(box) + [0, 0, w, h])[:4]
    x0, x1 = sorted((max(0, min(x0, w)), max(0, min(x1, w))))
    y0, y1 = sorted((max(0, min(y0, h)), max(0, min(y1, h))))
    return [x0, y0, x1, y1]


async def with_fast_fallback(call):
    """Run a Claude call in fast mode; if fast mode is rate-limited or rejected, rerun at standard speed."""
    try:
        return await call(fast_kwargs())
    except (anthropic.RateLimitError, anthropic.BadRequestError) as e:
        if not fast_kwargs():
            raise
        print(f"fast mode unavailable ({type(e).__name__}), retrying at standard speed")
        return await call({})


# ---------------------------------------------------------------- 1. spot (streamed) + describe

# Plain lines, not JSON: with a JSON list the model regularly closed the list after
# the first box (8/16 runs); with one line per piece it boxed every piece (16/16)
# and the first box arrives ~1-2s in.
SPOT_PROMPT = """This is a phone screenshot from Instagram. The user wants to shop the outfit.

Box every shoppable piece worn by the main person: clothing, shoes, socks, bags, hats, eyewear, belts, and clearly visible jewelry. Go through the whole outfit; a typical look has 3-6 pieces. At most 8, most prominent first. Include pieces cut off by the frame edge if you can tell what they are. Ignore the app UI, background, and other people.

Output format, nothing else:
handle: <creator's Instagram handle if visible on screen, else none>
<label> | <#RRGGBB> | <x0>,<y0>,<x1>,<y1> | <category>
(one line per piece; label is 1-2 words; box is tight pixel coordinates in this {w}x{h} image; category is one of {categories})"""

DESCRIBE_PROMPT = """This is one piece cropped from an outfit photo: "{label}" ({category}).

Write what a shopper would type to find this exact piece. Look closely for logos, monograms, signature designs and hardware that identify the brand."""


CATEGORIES = Spot.model_fields["category"].annotation.__args__
HEX = re.compile(r"#[0-9a-fA-F]{6}")


def parse_spot_line(line: str) -> Spot | None:
    """'Bear sweater | #3E1A1E | 26,20,486,450 | top' -> Spot, or None if the line isn't a piece."""
    parts = [x.strip() for x in line.split("|")]
    if len(parts) != 4:
        return None
    label, color, box, category = parts
    label = label.lstrip("-*•0123456789. ").strip()
    nums = [int(float(n)) for n in re.findall(r"-?\d+(?:\.\d+)?", box)]
    if len(nums) != 4 or not label:
        return None
    category = category.lower()
    return Spot(
        label=label[:1].upper() + label[1:],
        color_hex=color if HEX.fullmatch(color) else "#888888",
        box=nums,
        category=category if category in CATEGORIES else "accessory",
    )


async def detect_pieces(img: Image.Image, meta: dict) -> AsyncIterator[Spot]:
    """Yield each piece's box the moment its line is written. Fills meta['creator'] as it goes."""
    w, h = img.size
    content = [image_block(to_jpeg(img)),
               {"type": "text", "text": SPOT_PROMPT.format(w=w, h=h, categories=", ".join(CATEGORIES))}]
    queue: asyncio.Queue = asyncio.Queue()

    async def handle(line: str):
        line = line.strip()
        if line.lower().startswith("handle:"):
            handle = line.split(":", 1)[1].strip().lstrip("@")
            meta["creator"] = None if handle.lower() in ("", "none", "null", "n/a") else handle
        elif (spot := parse_spot_line(line)) is not None:
            await queue.put(spot)

    async def run(extra: dict):
        buf = ""
        async with claude().beta.messages.stream(
            model=MODEL,
            max_tokens=2000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_config={"effort": os.getenv("SPOT_EFFORT", "low")},
            messages=[{"role": "user", "content": content}],
            **extra,
        ) as stream:
            async for text in stream.text_stream:
                buf += text
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    await handle(line)
            await handle(buf)
            final = await stream.get_final_message()
        if final.stop_reason == "refusal":
            raise RuntimeError("model declined this image")

    async def runner():
        try:
            await with_fast_fallback(run)
            await queue.put(None)
        except Exception as e:
            await queue.put(e)

    task = asyncio.create_task(runner())
    try:
        count = 0
        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            count += 1
            if count > 8:
                continue
            item.box = clamp_box(item.box, w, h)
            yield item
    finally:
        task.cancel()


async def describe(shot_jpeg: bytes, crop_jpeg: bytes, spot: Spot) -> Details:
    """Search text for one piece. Started the moment its box streams in, so it's usually ready before the tap."""
    content = [image_block(crop_jpeg),
               {"type": "text", "text": DESCRIBE_PROMPT.format(label=spot.label, category=spot.category)}]

    async def call(extra: dict):
        return await claude().beta.messages.parse(
            model=MODEL,
            max_tokens=2000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": content}],
            output_format=Details,
            **extra,
        )

    resp = await with_fast_fallback(call)
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        # Fall back to a plain-label search rather than failing the piece.
        return Details(brand_guess=None, search_query=f"women's {spot.label}", broad_query=spot.label,
                       description=spot.label)
    return resp.parsed_output


# ---------------------------------------------------------------- 3. search

def _price(item: dict) -> tuple[float | None, str | None]:
    p = item.get("price")
    if isinstance(p, dict):
        label = p.get("value")
        return p.get("extracted_value"), label.rstrip("* ").strip() if isinstance(label, str) else label
    if item.get("extracted_price") is not None:
        return item["extracted_price"], item.get("price")
    return None, p if isinstance(p, str) else None


def _host(url: str | None) -> str:
    return urlparse(url or "").netloc.lower().removeprefix("www.")


async def serp(params: dict) -> dict:
    params = {**params, "api_key": os.environ["SERPAPI_API_KEY"], "hl": "en", "country": os.getenv("COUNTRY", "us")}
    r = await http().get("https://serpapi.com/search.json", params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def _clean(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in rows:
        for k in ("title", "source"):
            if isinstance(c.get(k), str):
                c[k] = html.unescape(c[k])
        if not c["link"] or not c["thumbnail"] or any(s in _host(c["link"]) for s in SOCIAL_HOSTS):
            continue
        key = (c["title"] or "").lower()[:60] + _host(c["link"])
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


async def lens_search(crop_url: str) -> list[dict]:
    data = await serp({"engine": "google_lens", "url": crop_url, "type": "visual_matches"})
    rows = []
    for m in data.get("visual_matches", [])[:20]:
        value, label = _price(m)
        rows.append({"title": m.get("title"), "source": m.get("source"), "link": m.get("link"),
                     "thumbnail": m.get("thumbnail") or m.get("image"), "price": value, "price_label": label, "via": "lens",
                     "in_stock": m.get("in_stock"),  # True / False / None (merchant didn't say)
                     "condition": "pre-owned" if m.get("second_hand_condition") else None,
                     "old_price": None})
    return _clean(rows)


async def shopping_search(piece: Details) -> list[dict]:
    query = piece.search_query
    if piece.brand_guess and piece.brand_guess.lower() not in query.lower():
        query = f"{piece.brand_guess} {query}"
    rows = await _shopping(query)
    if len(rows) < 6:  # over-specific query; widen it
        rows = _clean(rows + await _shopping(piece.broad_query))
    return rows


async def _shopping(query: str) -> list[dict]:
    data = await serp({"engine": "google_shopping", "q": query, "gl": os.getenv("COUNTRY", "us")})
    rows = []
    for m in data.get("shopping_results", [])[:12]:
        value, label = _price(m)
        rows.append({"title": m.get("title"), "source": m.get("source"), "link": m.get("product_link") or m.get("link"),
                     "thumbnail": m.get("thumbnail"), "price": value, "price_label": label, "via": "shopping",
                     "in_stock": m.get("in_stock"),  # Google Shopping lists offers for sale, so usually absent
                     "condition": "pre-owned" if m.get("second_hand_condition") else None,
                     "old_price": m.get("old_price")})
    return _clean(rows)


def merge_candidates(lens: list[dict], shop: list[dict]) -> list[dict]:
    """Lens first (best for exact matches), shopping fills the rest; capped to keep verification fast."""
    merged = _clean(lens[:8] + shop[:8] + lens[8:])
    return merged[:MAX_CANDIDATES]


def preliminary(lens: list[dict], shop: list[dict]) -> dict[str, list[dict]]:
    """Unverified results shown while Claude is still grading: Lens order for closest, shopping for the rest."""
    budget = float(os.getenv("BUDGET_PRICE", "50"))
    lens, shop = [r for r in lens if available(r)], [r for r in shop if available(r)]
    priced_lens = [r for r in lens if r["price"] is not None]
    return {
        "closest": [{**r, "match": "pending", "score": 0} for r in (priced_lens or lens)[:6]],
        "similar": [{**r, "match": "pending", "score": 0} for r in shop[:8]],
        "under": [{**r, "match": "pending", "score": 0} for r in (lens + shop) if r["price"] is not None and r["price"] < budget][:8],
    }


async def fetch_thumb(url: str) -> bytes | None:
    try:
        r = await http().get(url, timeout=4, follow_redirects=True)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        img.thumbnail((THUMB_EDGE, THUMB_EDGE))
        return to_jpeg(img, 75)
    except Exception:
        return None


# ---------------------------------------------------------------- 4. verify

VERIFY_PROMPT = """The first image is a clothing piece cropped from an influencer's photo: {description}{brand_line}

Below are numbered product candidates. Compare each candidate's photo to the target piece and grade it:
- "exact": the same product (same design, color, details)
- "close": nearly identical design and color; a shopper would take it as "that look"
- "similar": same type and vibe, but clearly different color, cut or details
- "unrelated": different kind of item, or not a reasonable alternative

Judge from the photos first; titles can mislead. A dupe or knockoff of a branded piece is at most "close", never "exact". is_product is false for anything that isn't a buyable listing. Grade every candidate."""


async def verify(crop_jpeg: bytes, piece: Details, candidates: list[dict]) -> list[dict]:
    thumbs = await asyncio.gather(*(fetch_thumb(c["thumbnail"]) for c in candidates))
    usable = [(c, t) for c, t in zip(candidates, thumbs) if t]
    if not usable:
        return []

    brand_line = f"\nThe original is by {piece.brand_guess}." if piece.brand_guess else ""
    prompt = VERIFY_PROMPT.format(description=piece.description, brand_line=brand_line)
    content: list[dict] = [image_block(crop_jpeg), {"type": "text", "text": prompt}]
    for i, (c, t) in enumerate(usable):
        content.append({"type": "text", "text": f"Candidate {i}: {c['title']} | {c['source']} | {c['price_label'] or 'no price'}"})
        content.append(image_block(t))

    async def call(extra: dict):
        return await claude().beta.messages.parse(
            model=MODEL,
            max_tokens=4000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": content}],
            output_format=Grades,
            **extra,
        )

    resp = await with_fast_fallback(call)
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError(f"verification returned nothing (stop_reason={resp.stop_reason})")
    by_index = {g.index: g for g in resp.parsed_output.grades}

    results = []
    for i, (c, _) in enumerate(usable):
        g = by_index.get(i)
        if g and g.match != "unrelated" and g.is_product and (g.match != "similar" or g.score >= 40):
            results.append({**c, "match": g.match, "score": g.score})
    results.sort(key=lambda r: ({"exact": 0, "close": 1, "similar": 2}[r["match"]], -r["score"]))
    return results


def available(r: dict) -> bool:
    return r.get("in_stock") is not False


def bucket(results: list[dict]) -> dict[str, list[dict]]:
    """Sold-out items are dropped, except exact matches: knowing the exact piece is sold out is
    still useful, so those stay in Closest, below everything you can actually buy."""
    budget = float(os.getenv("BUDGET_PRICE", "50"))
    buyable = [r for r in results if available(r)]
    sold_out_exact = [r for r in results if not available(r) and r["match"] == "exact"]
    return {
        "closest": [r for r in buyable if r["match"] in ("exact", "close")] + sold_out_exact,
        "similar": [r for r in buyable if r["match"] == "similar"],
        "under": sorted((r for r in buyable if r["price"] is not None and r["price"] < budget), key=lambda r: -r["score"]),
    }
