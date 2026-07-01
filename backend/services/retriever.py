import hashlib
import os
from typing import Dict, List

import chromadb

from services import sqlite_store
from services.site_utils import BOUNDARY_MODE, build_site_key

CHROMA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "chroma_data")

_client = None


def get_client():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=CHROMA_PATH)
    return _client


def site_key_to_collection_name(site_key: str) -> str:
    digest = hashlib.md5(site_key.encode()).hexdigest()[:24]
    return f"site_{digest}"


def reset_site_collection(site_key: str) -> None:
    client = get_client()
    collection_name = site_key_to_collection_name(site_key)
    try:
        client.delete_collection(name=collection_name)
    except Exception:
        pass


def store_site_chunks(
    site_key: str,
    chunk_records: List[Dict],
    embeddings: List[List[float]],
) -> int:
    if not chunk_records:
        return 0

    client = get_client()
    collection = client.get_or_create_collection(
        name=site_key_to_collection_name(site_key),
        metadata={"site_key": site_key},
    )
    collection.upsert(
        ids=[record["id"] for record in chunk_records],
        documents=[record["text"] for record in chunk_records],
        embeddings=embeddings,
        metadatas=[record["metadata"] for record in chunk_records],
    )
    return len(chunk_records)


def query_site(site_key: str, query_embedding: List[float], top_k: int = 8) -> List[Dict]:
    client = get_client()
    try:
        collection = client.get_collection(name=site_key_to_collection_name(site_key))
    except Exception:
        return []

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        include=["documents", "metadatas", "distances"],
    )
    documents = (results.get("documents") or [[]])[0]
    metadatas = (results.get("metadatas") or [[]])[0]
    distances = (results.get("distances") or [[]])[0]

    payload = []
    for index, document in enumerate(documents):
        payload.append(
            {
                "document": document,
                "metadata": metadatas[index] if index < len(metadatas) else {},
                "distance": distances[index] if index < len(distances) else None,
            }
        )
    return payload


def site_chunk_count(site_key: str) -> int:
    client = get_client()
    try:
        collection = client.get_collection(name=site_key_to_collection_name(site_key))
        return collection.count()
    except Exception:
        return 0


def resolve_site_for_url(url: str) -> Dict:
    sqlite_store.init_db()
    site_key = build_site_key(url, BOUNDARY_MODE)
    site = sqlite_store.get_site(site_key)
    if site:
        return {
            "site_key": site_key,
            "site": site,
            "job": sqlite_store.get_latest_job_for_site(site_key),
        }
    return {"site_key": site_key, "site": None, "job": None}


def check_exists(url: str) -> Dict:
    resolved = resolve_site_for_url(url)
    site = resolved["site"]
    job = resolved["job"]
    site_key = resolved["site_key"]

    if not site:
        return {"exists": False, "status": "missing", "site_key": site_key}

    chunk_count = int(site.get("chunk_count") or 0)
    exists = site.get("status") in ("completed", "completed_with_issues") and chunk_count > 0
    response = {
        "exists": exists,
        "status": site.get("status"),
        "site_key": site_key,
        "chunk_count": chunk_count,
        "html_pages": int(site.get("html_pages") or 0),
        "pdf_count": int(site.get("pdf_count") or 0),
        "job_id": site.get("last_job_id"),
    }
    if job:
        response.update(
            {
                "pages_discovered": int(job.get("pages_discovered") or 0),
                "pages_crawled": int(job.get("pages_crawled") or 0),
                "pdfs_discovered": int(job.get("pdfs_discovered") or 0),
                "pdfs_processed": int(job.get("pdfs_processed") or 0),
                "chunks_written": int(job.get("chunks_written") or 0),
                "failures": int(job.get("failures") or 0),
                "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"),
            }
        )
    return response
