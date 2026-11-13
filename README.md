# seen

Press the Action Button (or double-tap the back of the phone) on any Instagram outfit and get every piece matched to products you can buy.

```
Action Button → iOS Shortcut: Take Screenshot → POST /api/analyze → opens /look/<id>
                                                      │
                     Claude: find each piece + box ───┤
                     crop each piece                  │
                     Google Lens (crop) + Shopping ───┤  per piece, in parallel
                     Claude: grade every candidate ───┘  exact / close / similar / drop
```

## 1. Run it

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env     # then fill in the keys
.\.venv\Scripts\python -m uvicorn main:app --app-dir backend --port 8000
```

`$env:MOCK="1"` before starting returns fake results, so you can try the flow and UI with no API keys.

## 2. Give it a public URL

Your phone needs to reach the server, and Google Lens has to fetch the cropped images from it, so `localhost` won't work. For development:

```powershell
winget install Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:8000
```

Put the `https://….trycloudflare.com` URL it prints into `PUBLIC_BASE_URL` in `.env` and restart the server. (The URL changes every time cloudflared restarts. Later, deploy to Render, Railway or Fly to get a fixed one.)

## 3. Make the "Find outfit" shortcut (on the iPhone, one time)

In the Shortcuts app, create a new shortcut named **Find outfit** with these 6 actions:

1. **Take Screenshot**
2. **Resize Image**: Width `1080` (height auto)
3. **Convert Image**: to `JPEG`, quality about 80%. (Steps 2 and 3 shrink the upload from ~4 MB to ~200 KB, which saves 1-3 s on cellular.)
4. **Get Contents of URL**
   - URL: `https://YOUR-PUBLIC-URL/api/analyze`
   - Method: `POST`
   - Headers: `X-App-Token` = your `APP_TOKEN`
   - Request Body: `Form`, add a **File** field named `image` with the value **Converted Image**
5. **Get Dictionary Value**: `url` from **Contents of URL**
6. **Open URLs**: **Dictionary Value**

Then set the trigger:
- **Action Button** (iPhone 15 Pro and newer): Settings › Action Button › Shortcut › Find outfit
- **Back Tap** (any iPhone 8 or newer): Settings › Accessibility › Touch › Back Tap › Double Tap › Find outfit

To share it with others, tap Share › Copy iCloud Link on the shortcut and paste the link into `web/config.json` (`shortcutUrl`). The "Get the shortcut" button on the home page will then link to it.

## Speed

Expected timing on a good connection (the server prints the real timings for every look):

| moment | when |
|---|---|
| results page open, screenshot on screen | ~1 s |
| first box drawn | ~2-3 s |
| first products listed (Lens ranking) | ~4-6 s |
| verified matches for every piece | ~7-10 s |

How: pieces stream out of Claude one at a time and each one starts searching immediately; Lens and Shopping run in parallel; Lens results show before verification; Claude runs at low effort in fast mode (`FAST_MODE=0` in `.env` turns fast mode off and halves the Claude cost).

## Costs

Each look uses 1 Claude call to detect the pieces, plus 1 Claude call and 2 SerpAPI searches per piece. The SerpAPI free tier allows 250 searches a month, which is about 30 looks.

## Files

- `backend/pipeline.py`: detect → crop → search → verify (all the matching logic)
- `backend/main.py`: API, in-memory look store (expires after an hour), static files
- `web/`: home/onboarding page, the reading + results screen, styles
