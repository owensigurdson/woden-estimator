import base64
import json
import os
import re
import sys
import webbrowser
from pathlib import Path
from threading import Timer
from typing import List

import anthropic
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

load_dotenv()

api_key = os.getenv("ANTHROPIC_API_KEY")
if not api_key:
    print("\nERROR: ANTHROPIC_API_KEY not set.")
    print("Check your .env file.\n")
    sys.exit(1)

client = anthropic.Anthropic(api_key=api_key)
app = FastAPI()

class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in ("/favicon.ico",):
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                creds = base64.b64decode(auth[6:]).decode("utf-8")
                username, password = creds.split(":", 1)
                expected = os.getenv("APP_PASSWORD", "")
                if username == "woden" and password == expected and expected:
                    return await call_next(request)
            except Exception:
                pass
        return Response("Unauthorized", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Woden Estimator"'})

app.add_middleware(BasicAuthMiddleware)

# Labour multipliers applied by Python — Claude never touches these.
# Key = substring to match in section name (lowercase). First match wins.
# Calibrated so section totals reflect real installation time per $ of material.
LABOUR_MULTIPLIERS = {
    "fence": {
        # Boards are fast to nail — dominant material cost, minimal install time
        "board":      0.2,
        "cladding":   0.2,
        "picket":     0.2,
        "panel":      0.3,
        # Posts/foundation — moderate labour per post
        "foundation": 1.2,
        "post":       1.2,
        "footing":    1.2,
        # Rails — moderate, attaches quickly once posts are up
        "framing":    0.8,
        "rail":       0.8,
        # Gates and hardware
        "gate":       0.8,
        "hardware":   0.3,
    },
    "deck": {
        # Foundation — 4ft frost-line holes + sonotubes + concrete: cheap materials,
        # very high labour ratio. ~$625/footing all-in to customer.
        "foundation": 2.5,
        "footing":    2.5,
        "post":       2.5,
        "sonotube":   2.5,
        "concrete":   2.5,
        # Framing — ledger attachment, beam assembly, joist installation, hardware
        "framing":    1.2,
        "joist":      1.2,
        "beam":       1.2,
        "ledger":     1.2,
        # Decking boards — spacing, screwing, cutting. More precise than fence boards.
        "decking":    0.8,
        "board":      0.8,
        # Railings, stairs, hardware
        "railing":    0.8,
        "rail":       0.8,
        "stair":      1.0,
        "hardware":   0.3,
    },
    "landscape": {
        "sod":      1.5,
        "topsoil":  0.5,
        "mulch":    0.5,
        "delivery": 0.0,
    },
}
DEFAULT_LABOUR = 0.8


def get_labour_mult(job_type: str, section_name: str) -> float:
    table = LABOUR_MULTIPLIERS.get(job_type, {})
    name_lower = section_name.lower()
    for key, mult in table.items():
        if key in name_lower:
            return mult
    return DEFAULT_LABOUR


def run_market_check(job_data: dict, estimate_data: dict) -> dict | None:
    job_type = job_data.get("job_type", "deck")
    total = estimate_data.get("total", 0)
    sections_summary = [
        {"name": s["name"], "total": s.get("total", 0)}
        for s in estimate_data.get("sections", [])
        if not s.get("tbd") and s.get("total", 0) > 0
    ]

    prompt = f"""You are a construction cost analyst for Calgary and the surrounding Alberta market, 2025–2026.

JOB DETAILS:
{json.dumps(job_data, indent=2)}

ESTIMATE PRODUCED (all figures include OH, profit, and GST):
Total: ${total:,}
Sections:
{json.dumps(sections_summary, indent=2)}

TASK:
1. Determine the standard unit for this job type (sqft for decks and landscaping, LF for fences).
2. State the Calgary/AB market range: low (budget), average (mid-market), high (premium) in both $/unit and total dollars for this specific job size and spec.
3. For each section in the estimate, state what that section typically costs in the Calgary market.
4. Flag any section OR the overall total where the estimate deviates more than 11% above or below market average.

Return ONLY a raw JSON object — no prose, no code fences:
{{
  "unit": "sqft",
  "job_size": 100,
  "market_low_total": 7000,
  "market_avg_total": 9500,
  "market_high_total": 13000,
  "market_low_per_unit": 70,
  "market_avg_per_unit": 95,
  "market_high_per_unit": 130,
  "estimate_total": 10750,
  "estimate_per_unit": 107.5,
  "overall_deviation_pct": 13.2,
  "overall_flagged": true,
  "overall_flag_direction": "high",
  "sections": [
    {{
      "name": "Foundation",
      "estimate_total": 1222,
      "market_avg": 1100,
      "deviation_pct": 11.1,
      "flagged": true,
      "flag_direction": "high"
    }}
  ],
  "summary": "One sentence describing overall market position."
}}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            temperature=0,
            messages=[{"role": "user", "content": prompt}]
        )
        reply = response.content[0].text if response.content else ""
        m = re.search(r'\{[\s\S]*\}', reply)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    return None


def apply_margins(data: dict, job_type: str, oh_pct: float, profit_pct: float, gst_pct: float) -> dict:
    subtotal = 0
    for section in data.get("sections", []):
        if section.get("tbd"):
            section["total"] = 0
            continue
        mat = section.get("materials_cost", 0)
        mult = get_labour_mult(job_type, section["name"])
        base = mat * (1 + mult)
        total = round(base * (1 + oh_pct / 100) * (1 + profit_pct / 100))
        section["total"] = total
        subtotal += total

    gst_amount = round(subtotal * gst_pct / 100)
    data["subtotal"] = subtotal
    data["gst_pct"] = gst_pct
    data["gst_amount"] = gst_amount
    data["total"] = subtotal + gst_amount
    return data


def load_prices() -> str:
    try:
        data = json.loads(Path("prices.json").read_text(encoding="utf-8"))
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"(prices.json not found: {e})"


def build_system_prompt() -> str:
    prices = load_prices()
    return f"""You are an internal estimating assistant for Woden Contracting — a decks, fences, and landscaping contractor in Alberta, Canada. You generate detailed, layer-by-layer material takeoffs for Owen.

━━━ CURRENT SUPPLIER PRICES ━━━
{prices}

━━━ ALBERTA BUILDING CODE COMPLIANCE (2023 NBC Alberta Edition) ━━━

These are hard requirements — not suggestions. Every estimate must be spec'd to meet them. Flag any non-conformance in notes[].

FOOTINGS & FOUNDATION:
- Minimum footing depth: 1.2 m (4 ft) below finished grade — Alberta frost line. Never less.
- Footings must bear on undisturbed soil or engineered fill
- Decks elevated more than 1.8 m (6 ft) above finished ground: foundation must conform to NBC 9.12.2.2 (engineered or prescriptive foundation — flag this in notes if it applies)

GUARDS (Railings) — Section 9.8.8:
- Guard required on any side of a deck/balcony/landing where the drop exceeds 600 mm (24")
- Guard height (measured from walking surface):
    • Deck platform < 1.8 m (6 ft) high: minimum 914 mm (36")
    • Deck platform ≥ 1.8 m (6 ft) high: minimum 1067 mm (42")
- Deck ≤ 4.2 m above adjacent grade: horizontal rails and cable railing ARE permitted (9.8.8.6)
- Deck > 4.2 m above adjacent grade: no member, attachment, or opening between 140 mm and 900 mm above the deck surface may facilitate climbing — horizontal rails and cable are NOT permitted at this height
- If the job includes railings, check the deck height and spec the correct minimum height; note the permitted infill style

STAIRS — Section 9.8.4 (residential):
- Maximum rise: 200 mm (7-7/8")
- Minimum rise: 125 mm (5")
- Minimum run (tread depth): 235 mm (9-1/4")
- Minimum stair width: 860 mm (34")
- Handrail required when stairs have 3 or more risers
- When calculating stair stringers: verify rise × number of steps = total elevation change; flag if stringer length seems undersized

STRUCTURAL SIZING:
- Posts: 6x6 for all deck builds (Woden standard, exceeds code minimum)
- Beams and joists must be sized per NBC span tables for the given tributary width and species/grade; flag ESTIMATE on any member sizing not verified against tables
- Joist bearing: minimum 38 mm (1.5") on each support; use joist hangers at ledger and beam connections
- Ledger attachment to house: lag screws or through-bolts into rim joist; never attach to exterior cladding alone — note this in internal[]

FENCES (only apply these rules when the job_type is fence):
- Alberta Building Code does NOT govern fences; no ABC permit required
- Fences are regulated by municipal zoning bylaws (varies by city)
- Edmonton: development permit required for fences > 1.3 m in front/flanking yard, or > 2.0 m elsewhere
- Flag in notes[] ONLY for fence jobs: "Fence permit requirements vary by municipality — client to verify with local zoning office"

━━━ TAKEOFF RULES ━━━

DECK — FOUNDATION:
- Default post spacing: 8 ft max along beams; footings at each post location
- Ledger-mounted deck: footings only on outer beam side
- Free-standing: footings on both beam lines
- Each footing: 1 sonotube (use 10" for decks ≤ 12ft wide, 12" for wider) + 3 bags fast-set concrete per 4ft depth
- 1× ABA66 post base per footing (always 6x6 posts)

DECK — FRAMING:
- Joists: 16" OC for PT decking; 12" OC for Trex
- Joist count = (deck length / spacing in ft) + 1, plus doubled joists at each end = +2
- Beam sizing: 3-ply 2x10 for spans up to 14ft; 3-ply 2x12 for 14-20ft
- Posts: always 6x6 for all deck heights — low deck = 2ft posts, mid = 4ft, high = 6ft+
- 1× joist hanger per joist (both sides = double count if interior beam)
- Hardware allowance: add $150–$250 for screws, bolts, misc fasteners

DECK — DECKING BOARDS:
- 5/4x6 actual width = 5.5" + 1/8" gap = 5.625" per board
- Board count = (deck width in inches / 5.625) × (deck length / board length) + 10% waste, round up
- Default to 16ft boards if deck length ≤ 16ft, else 12ft boards in two runs
- Trex: same formula but use 12" OC joist spacing

DECK — STAIRS (per set):
- 2× 2x12 stringers per set (length = rise × steps / 8, round to next available length)
- 1 tread per step: use 5/4x6 PT (2 boards wide = 11") or composite if Trex deck
- 1× stringer hardware kit per set

FENCE — FOUNDATION:
- Post spacing: default 8ft (6ft for vinyl or high-wind areas)
- Post count = (linear ft / spacing) + 1, add 1 per gate
- Each post: 1 sonotube (8") + 2 bags fast-set concrete
- Post depth: min 3ft in Alberta frost line

FENCE — FRAMING:
- Wood/vinyl: 3 rails per bay (top, mid, bottom) for 6ft fence; 2 rails for 4ft
- Rail length = post spacing; count = (post count - 1) × rails per bay
- Use 2x4 rails for wood; vinyl rails come with panel system

FENCE — BOARDS:
- Wood 1×6 boards: actual 5.5" wide; boards per LF = 12 / 5.5 = 2.18 boards/LF + 5% waste
- Vinyl: panels sold per 8ft section = linear ft / 8, round up
- Chain link: sold per LF — match mesh to fence height

LANDSCAPING — QUANTITIES:
- Cubic yards of topsoil or mulch = (sqft × depth_inches) / 324
- Sod: sqft directly (include 5% waste)
- Add delivery charge if applicable (ask client — Bluegrass charges separately)

━━━ HOW TO ESTIMATE ━━━
1. Do the full material takeoff — count quantities, price each item from the supplier prices above.
2. Sum the raw material costs for each section. Output that sum as materials_cost.
3. DO NOT add labour, overhead, profit, or GST. The server calculates all of that automatically.
4. Return ONLY a single valid JSON object. No prose, no markdown, no code fences — raw JSON only.

━━━ OUTPUT (raw JSON, no other text) ━━━

{{
  "client": "<label or empty string>",
  "date": "<today as Month D, YYYY>",
  "summary": "<2-3 plain English sentences describing the job — no dollar figures>",
  "sections": [
    {{"name": "<Foundation|Framing|Decking|Railings|Stairs|Fence Boards|Gates & Hardware|Sod|Topsoil|Mulch|etc>", "materials_cost": <integer dollars>, "tbd": false}},
    ...
  ],
  "notes": [
    "<assumption or flag — one short sentence each>",
    "Estimate valid for 30 days.",
    "Pricing subject to site assessment."
  ],
  "internal": [
    "<one short line per section: key quantities and unit prices only — e.g. '22 posts × $11.53, 22 sonotubes × $12.99, 44 bags × $14.48 = $1,176'>"
  ]
}}

Rules:
- If a Home Rail price is null, set "tbd": true on that section and set materials_cost to 0
- Round all dollar values to nearest integer
- sections array: only include sections with actual scope (omit zero-cost ones)
- notes: keep each item to one short sentence
- internal: quantities × unit prices = section materials total only — no labour, no margins
- Do your math silently. Output ONLY the final JSON object — no prose before or after, no markdown fences"""


class Message(BaseModel):
    role: str
    content: str


class EstimateRequest(BaseModel):
    job_data: dict
    messages: List[Message]
    overhead_pct: float = 15.0
    profit_pct: float = 20.0
    gst_pct: float = 5.0


@app.post("/estimate")
async def estimate(req: EstimateRequest):
    try:
        system = build_system_prompt()

        job_json = json.dumps(req.job_data, indent=2)
        job_type = req.job_data.get("job_type", "deck")

        messages = [{"role": m.role, "content": m.content} for m in req.messages]

        from datetime import date as _date
        _d = _date.today()
        today = f"{_d.strftime('%B')} {_d.day}, {_d.year}"

        if not messages:
            user_content = f"Today's date is {today}. Generate a full estimate for this job:\n\n{job_json}"
            messages = [{"role": "user", "content": user_content}]

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=16000,
            temperature=1,
            thinking={"type": "enabled", "budget_tokens": 10000},
            system=system,
            messages=messages,
        )
        reply = next((b.text for b in response.content if b.type == "text"), "")
        Path("last_output.txt").write_text(reply, encoding="utf-8")

        clean = reply.strip()
        clean = re.sub(r'^```(?:json)?\s*', '', clean)
        clean = re.sub(r'\s*```$', '', clean).strip()
        # Always try to extract the outermost JSON object as a safety net
        m = re.search(r'\{[\s\S]*\}', clean)
        if m:
            clean = m.group(0)
        try:
            data = json.loads(clean)
            data = apply_margins(data, job_type, req.overhead_pct, req.profit_pct, req.gst_pct)
            mc = run_market_check(req.job_data, data)
            if mc:
                data["market_check"] = mc
            return {"ok": True, "data": data}
        except json.JSONDecodeError:
            return {"ok": False, "raw": reply}

    except Exception as e:
        return {"ok": False, "raw": f"Server error: {type(e).__name__}: {e}"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(Path(__file__).parent / "woden.ico", media_type="image/x-icon")

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return Path("index.html").read_text(encoding="utf-8")


def open_browser():
    webbrowser.open("http://localhost:8001")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8001))
    is_local = not os.getenv("PORT")
    print(f"\nWoden Estimator — starting on port {port}...")
    if is_local:
        print(f"Opening at http://localhost:{port}\n")
        Timer(1.2, open_browser).start()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
