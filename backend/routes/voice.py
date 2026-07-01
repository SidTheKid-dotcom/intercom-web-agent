from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from services.voice import synthesize_speech, transcribe_audio

router = APIRouter()


class VoiceSpeakRequest(BaseModel):
    text: str


class VoiceTranscribeResponse(BaseModel):
    transcript: str


@router.post("/voice/transcribe", response_model=VoiceTranscribeResponse)
async def voice_transcribe(
    request: Request,
    filename: str = Query(default="recording.webm"),
):
    audio_bytes = await request.body()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio payload")

    try:
        transcript = transcribe_audio(audio_bytes, filename=filename)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Transcription failed: {exc}")
    return VoiceTranscribeResponse(transcript=transcript)


@router.post("/voice/speak")
async def voice_speak(request: VoiceSpeakRequest):
    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail="Empty text")

    try:
        wav_bytes = synthesize_speech(request.text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Speech synthesis failed: {exc}")

    return Response(content=wav_bytes, media_type="audio/wav")
