"""
Real Seattle soft serve test — uses Brave Search + Cairn extract endpoint.
No mocks. Stops after 10 searches, 50 shops, or when seeing duplicates.
"""
from __future__ import annotations

import httpx
import asyncio
import json
import os
import sys
from typing import Dict, List, Set

CAIRN_URL = "http://localhost:8000"
CAIRN_KEY = "cairn-dev-key-001"
BRAVE_KEY = os.environ.get("BRAVE_API_KEY", "")

HEADERS = {"X-API-Key": CAIRN_KEY, "Content-Type": "application/json"}

# Skip non-useful domains
SKIP_DOMAINS = {
    "google.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "tiktok.com", "youtube.com", "pinterest.com", "tripadvisor.com",
    "maps.google.com", "doordash.com", "ubereats.com", "grubhub.com",
    "postmates.com", "yelp.com",  # yelp often blocks scraping
}


def should_skip(url: str) -> bool:
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.lower().replace("www.", "")
    return any(domain.endswith(d) for d in SKIP_DOMAINS)


async def brave_search(query: str, count: int = 8) -> List[Dict]:
    """Search via Brave API, return list of {url, title}."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": BRAVE_KEY, "Accept": "application/json"},
            params={"q": query, "count": count},
        )
        resp.raise_for_status()
        results = resp.json().get("web", {}).get("results", [])
        return [{"url": r["url"], "title": r.get("title", "")} for r in results]


async def run():
    if not BRAVE_KEY:
        print("ERROR: Set BRAVE_API_KEY env var")
        sys.exit(1)

    async with httpx.AsyncClient(headers=HEADERS, timeout=60.0) as client:
        print("\n═══════════════════════════════════════")
        print("  REAL TEST: Seattle Soft Serve Hunt")
        print("═══════════════════════════════════════\n")

        # 1. Create session
        resp = await client.post(f"{CAIRN_URL}/sessions", json={
            "goal_prompt": "Find all soft serve ice cream shops in Seattle, WA. "
                          "Identify distinct shops across all neighborhoods.",
            "goal_schema": {
                "dimensions": [
                    "shop_name", "neighborhood", "address",
                    "hours", "signature_flavors", "price_range"
                ]
            }
        })
        resp.raise_for_status()
        session_id = resp.json()["id"]
        print(f"  Session: {session_id}\n")

        all_shops: Dict[str, dict] = {}  # name -> data
        seen_urls: Set[str] = set()
        max_searches = 10
        max_shops = 50

        for iteration in range(1, max_searches + 1):
            print(f"── Search {iteration}/{max_searches} ─────────────────────────")

            # Get next query from Cairn
            resp = await client.get(f"{CAIRN_URL}/sessions/{session_id}/next-query")
            resp.raise_for_status()
            next_q = resp.json()

            if next_q.get("phrase") is None:
                print("  No more query angles. Done.")
                break

            phrase = next_q["phrase"]
            print(f"  Query: \"{phrase}\"")
            print(f"  Reasoning: {next_q.get('reasoning', '')}")

            # Search via Brave
            try:
                results = await brave_search(phrase)
            except Exception as e:
                print(f"  Search error: {e}")
                continue

            # Filter results
            urls = [r for r in results if not should_skip(r["url"])]
            print(f"  Results: {len(results)} total, {len(urls)} after filtering")

            # Log query
            await client.post(
                f"{CAIRN_URL}/sessions/{session_id}/queries",
                json={"phrase": phrase, "result_count": len(results)}
            )

            # Extract each page
            new_shops_this_round = 0
            for r in urls[:5]:  # max 5 pages per search
                url = r["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                print(f"\n  📄 Extracting: {r.get('title', url)[:60]}...")
                try:
                    resp = await client.post(
                        f"{CAIRN_URL}/sessions/{session_id}/pages/extract",
                        json={"url": url}
                    )
                    if resp.status_code != 200:
                        print(f"     ⚠️  Status {resp.status_code}: {resp.text[:100]}")
                        continue

                    data = resp.json()
                    extracted = data.get("extracted", {})

                    # Handle both single dict and listicle (array) responses
                    items = extracted if isinstance(extracted, list) else [extracted]
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        shop_name = item.get("shop_name")
                        if not shop_name:
                            continue
                        if shop_name not in all_shops:
                            all_shops[shop_name] = item
                            new_shops_this_round += 1
                            neighborhood = item.get("neighborhood", "?")
                            print(f"     🍦 NEW: {shop_name} ({neighborhood})")
                        else:
                            print(f"     ♻️  Already found: {shop_name}")

                    if not any(isinstance(i, dict) and i.get("shop_name") for i in items):
                        print(f"     ─  No shop extracted")

                except Exception as e:
                    print(f"     ❌ Error: {e}")

            print(f"\n  Round summary: {new_shops_this_round} new shops (total: {len(all_shops)})")

            if len(all_shops) >= max_shops:
                print(f"\n  Hit {max_shops} shops limit!")
                break

            # Check coverage every 3 iterations
            if iteration % 3 == 0:
                resp = await client.get(
                    f"{CAIRN_URL}/sessions/{session_id}/coverage",
                    params={"recompute": "true"}
                )
                if resp.status_code == 200:
                    cov = resp.json()
                    print(f"\n  📊 Coverage: {cov.get('overall_score', 0):.0%}")
                    rec = cov.get("recommendation", "continue")
                    print(f"  Recommendation: {rec.upper()}")
                    if rec == "stop":
                        print("  Cairn says stop!")
                        break

            print()

        # Final report
        print("\n═══════════════════════════════════════")
        print("  FINAL RESULTS")
        print(f"  Searches: {iteration}")
        print(f"  Shops found: {len(all_shops)}")
        print("═══════════════════════════════════════\n")

        for name, data in sorted(all_shops.items()):
            print(f"  🍦 {name}")
            if data.get("neighborhood"):
                print(f"     📍 {data['neighborhood']}")
            if data.get("address"):
                print(f"     🏠 {data['address']}")
            if data.get("hours"):
                hours = data["hours"]
                if isinstance(hours, dict):
                    hours = "; ".join(f"{k}: {v}" for k, v in list(hours.items())[:2])
                print(f"     🕐 {str(hours)[:80]}")
            if data.get("signature_flavors"):
                flavors = data["signature_flavors"]
                if isinstance(flavors, list):
                    flavors = ", ".join(flavors[:5])
                print(f"     🍨 {flavors}")
            if data.get("price_range"):
                print(f"     💰 {data['price_range']}")
            print()

        # Mark complete
        await client.patch(
            f"{CAIRN_URL}/sessions/{session_id}",
            json={"status": "completed"}
        )
        print("  Session complete. ✓")


if __name__ == "__main__":
    asyncio.run(run())
