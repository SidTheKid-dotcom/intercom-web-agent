import hashlib
import posixpath
import re
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    import tldextract  # type: ignore
except Exception:
    tldextract = None

_tldextractor = None


BOUNDARY_MODE = "domain_plus_subdomains"
TRACKING_QUERY_KEYS = {
    "gclid",
    "fbclid",
    "ref",
    "source",
    "mc_cid",
    "mc_eid",
}
COMMON_MULTI_PART_SUFFIXES = {
    "co.uk",
    "org.uk",
    "gov.uk",
    "ac.uk",
    "co.in",
    "org.in",
    "ac.in",
    "com.au",
    "net.au",
    "org.au",
}


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "https").lower()
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return url.strip()

    path = parsed.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    if path != "/":
        path = path.rstrip("/") or "/"
    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_QUERY_KEYS:
            continue
        query_items.append((key, value))
    query = urlencode(sorted(query_items))
    port = parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname

    return urlunparse((scheme, netloc, path, "", query, ""))


def get_registrable_domain(hostname: str) -> str:
    global _tldextractor
    hostname = (hostname or "").strip(".").lower()
    if not hostname:
        return hostname

    if tldextract is not None:
        try:
            if _tldextractor is None:
                _tldextractor = tldextract.TLDExtract(suffix_list_urls=None, cache_dir=False)
            parts = _tldextractor(hostname)
            if parts.domain and parts.suffix:
                return f"{parts.domain}.{parts.suffix}"
            if parts.domain:
                return parts.domain
        except Exception:
            pass

    labels = hostname.split(".")
    if len(labels) <= 2:
        return hostname

    suffix = ".".join(labels[-2:])
    if suffix in COMMON_MULTI_PART_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def build_site_key(url: str, boundary_mode: str = BOUNDARY_MODE) -> str:
    normalized = normalize_url(url)
    parsed = urlparse(normalized)
    root_domain = get_registrable_domain(parsed.hostname or "")
    return f"{root_domain}:{boundary_mode}"


def is_same_site_url(url: str, root_domain: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    if not hostname or not root_domain:
        return False
    return hostname == root_domain or hostname.endswith(f".{root_domain}")


def source_key_for(site_key: str, source_url: str, source_type: str, page_number: Optional[int] = None) -> str:
    digest = hashlib.sha1(f"{site_key}|{source_type}|{source_url}|{page_number or 0}".encode("utf-8")).hexdigest()
    return digest


def is_pdf_url(url: str) -> bool:
    path = (urlparse(url).path or "").lower()
    return path.endswith(".pdf")


def guess_title_from_url(url: str) -> str:
    path = urlparse(url).path or "/"
    leaf = posixpath.basename(path.rstrip("/"))
    if not leaf:
        return urlparse(url).hostname or url
    return leaf


def append_pdf_page(url: str, page_number: Optional[int]) -> str:
    if not page_number:
        return url
    return f"{url}#page={page_number}"


def unique_preserve_order(values: Iterable[str]) -> list[str]:
    seen = set()
    output = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output
