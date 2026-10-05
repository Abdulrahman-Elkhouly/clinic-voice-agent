"""The "front desk": serves the web page and hands out LiveKit tokens.

    python backend\server.py     then open http://localhost:8000
"""
import os
import uuid
from datetime import timedelta
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from livekit import api

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

app = FastAPI()


@app.get("/")
def index():
    # Job 2: serve the UI
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/token")
def token():
    # Job 1: sign a key card for one caller, one room, ten minutes
    room = f"support-{uuid.uuid4().hex[:8]}"         # a fresh room per call
    identity = f"caller-{uuid.uuid4().hex[:8]}"

    jwt = (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(identity)
        .with_name("Caller")
        .with_grants(api.VideoGrants(room_join=True, room=room))
        .with_ttl(timedelta(minutes=10))
        .to_jwt()
    )
    print(f"[TOKEN] {identity} -> {room}")
    # The URL isn't secret; the browser needs it to know where to connect
    return {"token": jwt, "url": os.environ["LIVEKIT_URL"], "room": room}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
