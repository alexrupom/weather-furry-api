from dotenv import load_dotenv
import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio

load_dotenv()

CORS_ORIGINS = os.getenv("CORS_ORIGINS", "").split(",")
FURRY_API_BASE_URL=os.getenv("FURRY_API_BASE_URL")
FURRY_API_KEY=os.getenv("FURRY_API_KEY")
WEATHER_API_BASE_URL=os.getenv("WEATHER_API_BASE_URL")
WEATHER_API_KEY=os.getenv("WEATHER_API_KEY")


app = FastAPI(title="Dev App-to-App API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/furry_weather")
async def get_furry_weather():
    async with httpx.AsyncClient(timeout=10) as client:
        async def get_furry_positions():
            r = await client.get(
                f"{FURRY_API_BASE_URL}/ferrypositions",
                headers={"Ocp-Apim-Subscription-Key": FURRY_API_KEY},
            )
            r.raise_for_status()
            return r.json()

        async def get_weather_now():
            r = await client.get(
                f"{WEATHER_API_BASE_URL}/current.json",
                headers={"key": WEATHER_API_KEY},
                params={"q": "Auckland"},
            )
            r.raise_for_status()
            return r.json()

        try:
            ferry_data, weather_data = await asyncio.gather(
                get_furry_positions(), get_weather_now()
            )
        except httpx.HTTPStatusError as e:
            # bubble up the upstream HTTP status for simplicity
            raise HTTPException(status_code=e.response.status_code, detail=str(e))
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Upstream error: {e!s}")
        print(ferry_data)
        print(weather_data)
    return {
        "ferry_positions": ferry_data,
        "weather": weather_data
    }
