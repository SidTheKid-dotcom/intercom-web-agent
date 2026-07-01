import os
import time
from typing import List
from google import genai
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "gemini-embedding-001"
CHUNK_SIZE = 2000  # characters (~500 tokens)
CHUNK_OVERLAP = 200
EMBEDDING_BATCH_SIZE = int(os.getenv("EMBEDDING_BATCH_SIZE", "5"))
EMBEDDING_MIN_INTERVAL_SECONDS = float(os.getenv("EMBEDDING_MIN_INTERVAL_SECONDS", "10"))
EMBEDDING_RETRY_SECONDS = [20, 40, 70]

_client = None
_last_embedding_request_at = 0.0


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
    return _client


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Split text into overlapping chunks."""
    if not text or not text.strip():
        return []

    # Clean up the text
    text = text.strip()

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk.strip())
        start += chunk_size - overlap

    return chunks


def _pace_embedding_request() -> None:
    global _last_embedding_request_at
    elapsed = time.monotonic() - _last_embedding_request_at
    delay = EMBEDDING_MIN_INTERVAL_SECONDS - elapsed
    if delay > 0:
        logger.info("Sleeping %.1fs to pace embedding requests...", delay)
        time.sleep(delay)
    _last_embedding_request_at = time.monotonic()


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Embed a list of texts using Gemini gemini-embedding-001."""
    if not texts:
        return []

    logger.info(f"Preparing to embed {len(texts)} chunks using {EMBEDDING_MODEL}")

    # Gemini embedding API supports batching
    # Process in much smaller batches. Free tier rejects massive 35k-token payloads in one request.
    all_embeddings = []
    batch_size = max(1, EMBEDDING_BATCH_SIZE)

    num_batches = (len(texts) + batch_size - 1) // batch_size

    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        total_chars = sum(len(chunk) for chunk in batch)
        estimated_tokens = total_chars // 4  # Rule of thumb: 1 token ~= 4 chars

        batch_num = (i // batch_size) + 1
        logger.info(f"Sending batch {batch_num}/{num_batches}: {len(batch)} chunks, {total_chars} chars, ~{estimated_tokens} tokens")
        for attempt in range(len(EMBEDDING_RETRY_SECONDS) + 1):
            try:
                _pace_embedding_request()
                result = _get_client().models.embed_content(
                    model=EMBEDDING_MODEL,
                    contents=batch,
                )
                all_embeddings.extend([e.values for e in result.embeddings])
                logger.info(f"Successfully received {len(batch)} embeddings. Total embedded: {len(all_embeddings)}/{len(texts)}")
                break
            except Exception as e:
                is_rate_limited = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)
                if is_rate_limited and attempt < len(EMBEDDING_RETRY_SECONDS):
                    delay = EMBEDDING_RETRY_SECONDS[attempt]
                    logger.warning("Embedding rate limit on batch %s. Sleeping %ss before retry...", batch_num, delay)
                    time.sleep(delay)
                    continue
                logger.error(f"Error embedding batch {batch_num}: {e}")
                raise e

    logger.info(f"Completed embedding {len(all_embeddings)} total chunks.")
    return all_embeddings
