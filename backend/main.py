import os
from dotenv import load_dotenv

# Load environment variables before anything else
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routes.ingest import router as ingest_router
from routes.chat import router as chat_router
from routes.voice import router as voice_router
from routes.live_agent import router as live_agent_router
from services.sqlite_store import init_db

app = FastAPI(title="PageChat API", version="0.1.0")

# CORS — allow all origins for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ingest_router)
app.include_router(chat_router)
app.include_router(voice_router)
app.include_router(live_agent_router)


@app.on_event("startup")
async def startup_event():
    init_db()


@app.get("/health")
async def health():
    return {"status": "ok"}
