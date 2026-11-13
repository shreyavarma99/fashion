"""seen - backend.

POST /api/analyze                    (iOS Shortcut) screenshot in, {id, url} out immediately;
                                     starts finding the pieces (boxes + labels) in the background.
GET  /api/looks/:id                  (web app) polled for pieces and results.
POST /api/looks/:id/pieces/:i/search (web app) user tapped a piece: search for that one only.
GET  /look/:id                       the capture + results screen.
GET  /media/:id/...                  screenshot + crops (Google Lens fetches crops from here).

Looks live in memory only and expire after LOOK_TTL_MIN minutes; nothing is written to disk.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
import traceback
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines() if (ROOT / ".env").exists() else []:
    key, sep, value = line.partition("=")
    if sep and not key.strip().startswith("#") and value.strip():
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

import pipeline  # noqa: E402  (after .env so the clients see the keys)

WEB = ROOT / "web"
TTL = int(os.getenv("LOOK_TTL_MIN", "60")) * 60
MAX_UPLOAD = 15 * 1024 * 1024
MOCK = os.getenv("MOCK") == "1"

app = FastAPI(title="seen")
LOOKS: dict[str, dict] = {}                       # id -> public state (what the page sees)
PRIVATE: dict[str, dict] = {}                     # id -> {"base": url, "pieces": [{"spot", "crop", "details": Task}]}
MEDIA: dict[str, bytes] = {}                      # "id/name.jpg" -> jpeg


def base_url(request: Request) -> str:
    return (os.getenv("PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")


def purge() -> None:
    cutoff = time.time() - TTL
    for lid in [k for k, v in LOOKS.items() if v["created"] < cutoff]:
        LOOKS.pop(lid, None)
        PRIVATE.pop(lid, None)
        for key in [k for k in MEDIA if k.startswith(lid + "/")]:
            MEDIA.pop(key, None)


def empty_results() -> dict:
    return {"closest": [], "similar": [], "under": []}


# ---------------------------------------------------------------- 1. capture: find the pieces

@app.post("/api/analyze")
async def analyze(
    request: Request,
    background: BackgroundTasks,
    image: UploadFile = File(...),
    x_app_token: str | None = Header(default=None),
):
    expected = os.getenv("APP_TOKEN")
    if expected and not secrets.compare_digest(x_app_token or "", expected):
        raise HTTPException(401, "bad token")
    raw = await image.read()
    if not raw or len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "image missing or too large")

    purge()
    try:
        img = pipeline.prepare_image(raw)
    except Exception:
        raise HTTPException(400, "not an image")

    lid = secrets.token_urlsafe(12)
    MEDIA[f"{lid}/shot.jpg"] = pipeline.to_jpeg(img)
    LOOKS[lid] = {
        "id": lid, "status": "reading", "created": time.time(),
        "width": img.width, "height": img.height, "shot": f"/media/{lid}/shot.jpg",
        "creator": None, "pieces": [], "error": None, "timings": {},
    }
    PRIVATE[lid] = {"base": base_url(request), "pieces": []}
    background.add_task(find_pieces, lid, img)
    return {"id": lid, "url": f"{base_url(request)}/look/{lid}"}


async def find_pieces(lid: str, img) -> None:
    """Stream pieces out of Claude; each one appears on screen the moment it's identified. No searching yet."""
    look, priv = LOOKS[lid], PRIVATE[lid]
    t0 = time.time()
    meta: dict = {}
    try:
        source = mock_pieces(img) if MOCK else pipeline.detect_pieces(img, meta)
        async for p in source:
            x0, y0, x1, y1 = p.box
            if (x1 - x0) * (y1 - y0) < 0.002 * img.width * img.height or len(look["pieces"]) >= 8:
                continue  # a sliver nobody could tap or search
            i = len(look["pieces"])
            crop = pipeline.to_jpeg(pipeline.crop_piece(img, p.box))
            MEDIA[f"{lid}/{i}.jpg"] = crop
            details = asyncio.create_task(mock_details(p) if MOCK else pipeline.describe(MEDIA[f"{lid}/shot.jpg"], crop, p))
            priv["pieces"].append({"spot": p, "crop": crop, "details": details})
            look["pieces"].append({
                "label": p.label, "category": p.category, "color": p.color_hex, "box": p.box,
                "crop": f"/media/{lid}/{i}.jpg", "status": "idle", "results": empty_results(),
            })
            look["timings"].setdefault("first_piece", round(time.time() - t0, 2))
        look["creator"] = meta.get("creator") or ("mira.wears" if MOCK else None)
        look["timings"]["all_pieces"] = round(time.time() - t0, 2)
        look["status"] = "found" if look["pieces"] else "empty"
    except Exception as e:
        traceback.print_exc()
        look.update(status="error", error=str(e))


# ---------------------------------------------------------------- 2. user picked a piece: search it

@app.post("/api/looks/{lid}/pieces/{i}/search")
async def search_piece(lid: str, i: int):
    look, priv = LOOKS.get(lid), PRIVATE.get(lid)
    if not look or not priv:
        raise HTTPException(404, "look expired or not found")
    if not 0 <= i < len(priv["pieces"]):
        raise HTTPException(404, "no such piece")
    state = look["pieces"][i]
    if state["status"] in ("idle", "error"):  # idempotent: double taps don't double-spend
        state["status"] = "searching"
        asyncio.create_task(run_search(lid, i))
    return {"status": state["status"]}


async def run_search(lid: str, i: int) -> None:
    look, priv = LOOKS[lid], PRIVATE[lid]
    entry = priv["pieces"][i]
    p, crop = entry["spot"], entry["crop"]
    state = look["pieces"][i]
    t0 = time.time()
    try:
        if MOCK:
            return await mock_search(lid, i, state)
        # Lens and Shopping race. Each batch goes on screen the moment it lands and is
        # verified on its own, so nothing waits for the slower search.
        found = {"lens": [], "shopping": []}
        verified: dict[str, list[dict] | None] = {}  # name -> graded rows (None = grading failed)

        def publish() -> None:
            graded = [r for rows in verified.values() if rows for r in rows]
            ungraded = {n: rows for n, rows in found.items() if n not in verified or verified[n] is None}
            if graded:
                seen = {r["link"] for r in graded}
                extra = pipeline.preliminary(ungraded.get("lens", []), ungraded.get("shopping", []))
                rank = {"exact": 0, "close": 1, "similar": 2}
                results = pipeline.bucket(sorted(graded, key=lambda r: (rank[r["match"]], -r["score"])))
                if any(v is None for v in verified.values()):  # grading failed for a batch: still show it
                    for k in results:
                        results[k] += [r for r in extra[k] if r["link"] not in seen]
                state["results"] = results
            else:
                state["results"] = pipeline.preliminary(found["lens"], found["shopping"])

        stages: dict[str, float] = {}
        stamp = lambda k: stages.setdefault(k, round(time.time() - t0, 1))  # noqa: E731

        async def search(name, coro):
            try:
                rows = await coro
                stamp(f"{name}_found")
            except Exception as e:
                print(f"{name} search failed: {e!r}")
                return
            found[name] = rows
            if not rows:
                return
            state["status"] = "ranking"
            state.setdefault("t_first", round(time.time() - t0, 2))
            publish()
            try:
                details = await entry["details"]
                stamp("details_ready")
                verified[name] = await pipeline.verify(crop, details, rows[: pipeline.MAX_CANDIDATES])
                stamp(f"{name}_verified")
            except Exception:
                traceback.print_exc()
                verified[name] = None
            publish()

        async def shopping():  # needs the search text; Lens doesn't, so Lens starts instantly
            details = await entry["details"]
            stamp("details_ready")
            return await pipeline.shopping_search(details)

        await asyncio.gather(
            search("lens", pipeline.lens_search(f"{priv['base']}/media/{lid}/{i}.jpg")),
            search("shopping", shopping()),
        )
        lens, shop = found["lens"], found["shopping"]
        if not lens and not shop:
            state["status"] = "error"
            return
        state["status"] = "done"
        state["t_done"] = round(time.time() - t0, 2)
        print(f"look {lid} piece {i} ({p.label}): first results {state['t_first']}s, verified {state['t_done']}s "
              f"(lens {len(lens)}, shopping {len(shop)}) stages={stages}")
    except Exception:
        traceback.print_exc()
        state["status"] = "error"


# ---------------------------------------------------------------- read endpoints

@app.get("/api/looks/{lid}")
async def get_look(lid: str):
    look = LOOKS.get(lid)
    if not look:
        raise HTTPException(404, "look expired or not found")
    return JSONResponse(look)


@app.get("/media/{lid}/{name}")
async def media(lid: str, name: str):
    data = MEDIA.get(f"{lid}/{name}")
    if data is None:
        raise HTTPException(404)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/look/{lid}")
async def look_page(lid: str):
    return FileResponse(WEB / "look.html")


@app.get("/health")
async def health():
    return {"ok": True}


# ---------------------------------------------------------------- MOCK=1: fake data, no API keys

async def mock_pieces(img):
    w, h = img.size
    for label, color, b in [("Blazer", "#a8865f", [.2, .3, .8, .62]), ("Knit top", "#e9e2d6", [.4, .32, .6, .45]),
                            ("Trousers", "#2f2f33", [.25, .6, .75, .95]), ("Bag", "#6e2a3c", [.72, .5, .86, .6])]:
        await asyncio.sleep(0.5)
        yield pipeline.Spot(label=label, color_hex=color, box=[int(b[0] * w), int(b[1] * h), int(b[2] * w), int(b[3] * h)],
                            category="top")


async def mock_details(spot):
    return pipeline.Details(brand_guess=None, search_query=spot.label, broad_query=spot.label, description=spot.label)


async def mock_search(lid: str, i: int, state: dict) -> None:
    await asyncio.sleep(1.0)
    thumb = f"/media/{lid}/{i}.jpg"
    rows = [{"title": f"Relaxed {state['label'].lower()} option {j + 1}", "source": src, "link": "https://example.com",
             "thumbnail": thumb, "price": price, "price_label": f"${price}", "match": m, "score": 95 - j * 7}
            for j, (src, price, m) in enumerate([("Northline", 228, "exact"), ("Atelier Nine", 195, "close"),
                                                 ("Fold Studio", 168, "close"), ("Mango", 49, "similar"), ("H&M", 34, "similar")])]
    state["results"] = pipeline.preliminary([{**r, "match": "pending"} for r in rows], rows[3:])
    state["status"] = "ranking"
    await asyncio.sleep(1.0)
    state["results"] = pipeline.bucket(rows)
    state["status"] = "done"


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
