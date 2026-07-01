"""
Web scraper for item research.
Source priority: EQL wiki → Allakhazam → general web (stop at first good hit).
Rate-limited with real User-Agent to avoid being blocked.
"""

import logging
import time
from typing import Optional
from urllib.parse import quote, quote_plus

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_MIN_SECONDS_BETWEEN_REQUESTS = 2.0
_last_request_time: float = 0.0


def _rate_limit():
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    wait = _MIN_SECONDS_BETWEEN_REQUESTS - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_request_time = time.monotonic()


def _fetch(url: str, timeout: int = 10) -> Optional[str]:
    _rate_limit()
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        log.warning("Scrape failed (%s): %s", url, e)
        return None


def _text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return " ".join(soup.get_text(separator=" ").split())[:4000]


def scrape_item(item_name: str, source_templates: list[str]) -> tuple[str, str]:
    """
    Try each source template in order; return (text_content, source_url) from
    the first that returns usable content, or ('', '') if all fail.
    """
    for template in source_templates:
        # A MediaWiki page title in the URL PATH (e.g. eqlwiki.com/Cloak_of_Flames)
        # uses underscores for spaces; an item in a QUERY STRING (e.g.
        # ?search=Cloak+of+Flames) uses + encoding. Decide by where {item} sits.
        before_item = template.split("{item}", 1)[0]
        if "?" in before_item:
            encoded = quote_plus(item_name)                          # query parameter
        else:
            encoded = quote(item_name.replace(" ", "_"), safe="")    # path segment
        url = template.replace("{item}", encoded)
        html = _fetch(url)
        if not html:
            continue
        text = _text_from_html(html)
        # Heuristic: if the page mentions the item name, it's probably relevant
        if item_name.lower() in text.lower():
            log.info("Scraped content for '%s' from %s", item_name, url)
            return text, url
        log.debug("Page at %s didn't mention '%s', trying next source", url, item_name)

    log.info("No scrape results found for '%s'", item_name)
    return "", ""
