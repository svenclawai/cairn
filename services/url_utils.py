from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

STRIP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "source", "mc_cid", "mc_eid",
}


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url.strip().lower())
    qs = {k: v for k, v in parse_qs(parsed.query).items() if k not in STRIP_PARAMS}
    clean = parsed._replace(
        scheme=parsed.scheme,
        netloc=parsed.netloc.lstrip("www."),
        query=urlencode(qs, doseq=True),
        fragment="",
    )
    return urlunparse(clean)
