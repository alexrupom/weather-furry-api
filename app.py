from dotenv import load_dotenv
import os, json, math, asyncio
from typing import Any, Dict, List, Tuple
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx

from google import genai

load_dotenv()

FURRY_API_BASE_URL = os.getenv("FURRY_API_BASE_URL")      # e.g. https://api.example.com
FURRY_API_KEY       = os.getenv("FURRY_API_KEY")
WEATHER_API_BASE_URL = os.getenv("WEATHER_API_BASE_URL")  # e.g. https://api.weatherapi.com/v1
WEATHER_API_KEY      = os.getenv("WEATHER_API_KEY")
GEMINI_API_KEY       = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL         = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

if not all([FURRY_API_BASE_URL, FURRY_API_KEY, WEATHER_API_BASE_URL, WEATHER_API_KEY, GEMINI_API_KEY]):
    raise RuntimeError("Missing one or more required environment variables in .env")

app = FastAPI(title="Dev App-to-App API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Wharf list (adjust to your routes) ----------
WHARVES: Dict[str, Tuple[float, float]] = {
    "Downtown Ferry Terminal": (-36.8440, 174.7680),
    "Devonport Wharf": (-36.8308, 174.7995),
    "Half Moon Bay": (-36.8747, 174.9025),
    "Hobsonville Wharf": (-36.7923, 174.6574),
    "West Harbour Marina": (-36.7902, 174.6589),
    "Matiatia (Waiheke)": (-36.7879, 174.9993),
    "Gulf Harbour": (-36.6202, 174.7948),
}

# Optional baseline speeds (km/h). Gemini will use/adjust these with weather.
BASELINE_SPEEDS = {
    "FULLERS": 28.0,
    "Sea Link": 20.0,
    "Sea Link (Commercial)": 18.0,
    "Belaire Ferries": 26.0,
    "Explore Group NZ": 26.0,
    "Island Direct": 24.0,
    "Cruise Ship": 18.0,
    "_default": 24.0,
}

# ---------- Helpers ----------
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a))

def nearest_wharf(lat: float, lon: float) -> Tuple[str, float]:
    best_name, best_d = None, float("inf")
    for name, (wlat, wlon) in WHARVES.items():
        d = haversine_km(lat, lon, wlat, wlon)
        if d < best_d:
            best_name, best_d = name, d
    return best_name, best_d

def build_items_for_gemini(ferry_list: List[Dict[str, Any]], weather: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    items = []
    for v in ferry_list:
        lat, lon = v["lat"], v["lng"]
        wharf_name, dist_km = nearest_wharf(lat, lon)
        items.append({
            "vessel": v.get("vessel"),
            "operator": v.get("operator"),
            "timestamp": v.get("timestamp"),
            "nearest_wharf": wharf_name,
            "distance_km": round(dist_km, 3),
        })
    w = weather["current"]
    weather_summary = {
        "condition": w["condition"]["text"],
        "wind_kph": w["wind_kph"],
        "gust_kph": w.get("gust_kph"),
        "precip_mm": w.get("precip_mm"),
        "humidity": w.get("humidity"),
        "temp_c": w.get("temp_c"),
        "is_day": w.get("is_day"),
        "visibility_km": w.get("vis_km"),
    }
    return items, weather_summary

def call_gemini(items: List[Dict[str, Any]], weather_summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Synchronous call to Gemini; we'll run it in a thread from the async route.
    Returns list of: {"vessel","nearest_wharf","eta_minutes","confidence","notes"}
    """
    client = genai.Client(api_key=GEMINI_API_KEY)

    system = (
        "You are a marine ops assistant. Estimate ETA (minutes) from current position "
        "to the provided nearest wharf for each vessel.\n"
        "Use distance_km and operator to infer speed, then adjust by weather.\n"
        "Baseline speeds (km/h):\n" + json.dumps(BASELINE_SPEEDS) + "\n"
        "Weather adjustments (multiplicative):\n"
        "- wind_kph ≥ 25 ⇒ -25% speed; wind_kph ≥ 10 ⇒ -10%\n"
        "- if condition contains rain/shower/storm ⇒ additional -10%\n"
        "- cap total slowdown so effective speed ≥ 50% of baseline.\n\n"
        "Return STRICT JSON ONLY as an array of objects with schema:\n"
        "{ \"vessel\": string, \"nearest_wharf\": string, "
        "\"eta_minutes\": number, \"confidence\": number, \"notes\": string }"
    )

    payload = {"items": items, "weather": weather_summary}

    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            {"role": "user", "parts": [{"text": system}]},
            {"role": "user", "parts": [{"text": json.dumps(payload)}]},
        ],
        config={
            "temperature": 0.2,
            "top_p": 0.9,
            "max_output_tokens": 1024,
            "response_mime_type": "application/json",
        },
    )
    
    # Extract text from the response correctly
    response_text = resp.text
    return json.loads(response_text)

def merge_etas_back(ferries: List[Dict[str, Any]], eta_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Build an index by (vessel, nearest_wharf)
    eta_idx = {(e["vessel"], e["nearest_wharf"]): e for e in eta_list}
    enriched = []
    for v in ferries:
        lat, lon = v["lat"], v["lng"]
        wharf_name, dist_km = nearest_wharf(lat, lon)
        e = eta_idx.get((v.get("vessel"), wharf_name))
        v2 = dict(v)
        v2["nearest_wharf"] = wharf_name
        v2["distance_km"] = round(dist_km, 3)
        if e:
            v2["eta"] = {
                "minutes": e.get("eta_minutes"),
                "confidence": e.get("confidence"),
                "notes": e.get("notes"),
            }
        enriched.append(v2)
    return enriched

# ---------- Your combined route + Gemini ETA ----------
@app.get("/furry_weather")
async def get_furry_weather():
    # async with httpx.AsyncClient(timeout=10) as client:
    #     async def get_furry_positions():
    #         r = await client.get(
    #             f"{FURRY_API_BASE_URL}/ferrypositions",
    #             headers={"Ocp-Apim-Subscription-Key": FURRY_API_KEY},
    #         )
    #         r.raise_for_status()
    #         return r.json()

    #     async def get_weather_now():
    #         r = await client.get(
    #             f"{WEATHER_API_BASE_URL}/current.json",
    #             headers={"key": WEATHER_API_KEY},
    #             params={"q": "Auckland"},
    #         )
    #         r.raise_for_status()
    #         return r.json()

    #     try:
    #         ferry_data, weather_data = await asyncio.gather(get_furry_positions(), get_weather_now())
    #     except httpx.HTTPStatusError as e:
    #         raise HTTPException(status_code=e.response.status_code, detail=str(e))
    #     except httpx.HTTPError as e:
    #         raise HTTPException(status_code=502, detail=f"Upstream error: {e!s}")

    # # Prepare items for Gemini
    # unfiltered_ferries = ferry_data.get("response")
    # desired_vessels = ['Kekeno', 'Kea', 'Kokako']
    # ferries = [d for d in unfiltered_ferries if d['vessel'] in desired_vessels]
    # if isinstance(ferries, dict) and "response" in ferries:
    #     ferries = ferries["response"]

    # items, weather_summary = build_items_for_gemini(ferries, weather_data)

    # # Call Gemini in a worker thread (SDK is sync)
    # try:
    #     eta_list = await asyncio.to_thread(call_gemini, items, weather_summary)
    # except Exception as e:
    #     # If Gemini fails, return the raw data but with computed nearest_wharf + distance
    #     enriched = []
    #     for v in ferries:
    #         wharf_name, dist_km = nearest_wharf(v["lat"], v["lng"])
    #         vv = dict(v)
    #         vv["nearest_wharf"] = wharf_name
    #         vv["distance_km"] = round(dist_km, 3)
    #         vv["eta"] = {"minutes": None, "confidence": 0.0, "notes": f"Gemini error: {e!s}"}
    #         enriched.append(vv)

    #     return {
    #       "ferry_positions": enriched,
    #       "weather": weather_data
    #     }

    # enriched = merge_etas_back(ferries, eta_list)

    # return {
    #     "ferry_positions": enriched,
    #     "weather": weather_data
    # }

    #TODO: Remove mock data when enabling live data above
    return {
        "ferry_positions": [
          {
            "mmsi": 512006003,
            "callsign": "ZMG3426",
            "eta": {
              "minutes": 0.44,
              "confidence": 0.95,
              "notes": "Normal operation.  Distance 0.207 km, operator FULLERS, baseline speed 28.0 km/h. Weather: Clear, wind 5.4 kph (no speed reduction)."
            },
            "lat": -36.842301666666664,
            "lng": 174.76706,
            "operator": "FULLERS",
            "timestamp": "2025-11-06T07:29:56.000Z",
            "vessel": "Kekeno",
            "nearest_wharf": "Downtown Ferry Terminal",
            "distance_km": 0.207
          }
        ],
        "weather": {
          "location": {
            "name": "Auckland",
            "region": "",
            "country": "New Zealand",
            "lat": -36.8667,
            "lon": 174.7667,
            "tz_id": "Pacific/Auckland",
            "localtime_epoch": 1762414710,
            "localtime": "2025-11-06 20:38"
          },
          "current": {
            "last_updated_epoch": 1762414200,
            "last_updated": "2025-11-06 20:30",
            "temp_c": 20.2,
            "temp_f": 68.4,
            "is_day": 0,
            "condition": {
              "text": "Clear",
              "icon": "//cdn.weatherapi.com/weather/64x64/night/113.png",
              "code": 1000
            },
            "wind_mph": 3.4,
            "wind_kph": 5.4,
            "wind_degree": 19,
            "wind_dir": "NNE",
            "pressure_mb": 1016.0,
            "pressure_in": 30.0,
            "precip_mm": 0.0,
            "precip_in": 0.0,
            "humidity": 73,
            "cloud": 25,
            "feelslike_c": 20.2,
            "feelslike_f": 68.4,
            "windchill_c": 17.0,
            "windchill_f": 62.6,
            "heatindex_c": 17.0,
            "heatindex_f": 62.6,
            "dewpoint_c": 13.4,
            "dewpoint_f": 56.1,
            "vis_km": 10.0,
            "vis_miles": 6.0,
            "uv": 0.0,
            "gust_mph": 6.2,
            "gust_kph": 10.0
          }
        }
      }
