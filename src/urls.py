"""Shared URL identity rules for history and external navigation."""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def canonical_url(url: str) -> str:
    if not str(url).strip():
        return ""
    parts = urlsplit(str(url).strip())
    host = (parts.hostname or "").lower()
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        match = re.search(r"/jobs/view/(?:[^/?#]+-)?(\d+)", parts.path)
        if match:
            return f"https://www.linkedin.com/jobs/view/{match.group(1)}/"
    ignored = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
               "source", "sourceid", "trackingid", "trk", "ref", "refid"}
    query = urlencode(sorted((key, value) for key, value in
                             parse_qsl(parts.query, keep_blank_values=True)
                             if key.lower() not in ignored))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path.rstrip("/") or "/", query, ""))
