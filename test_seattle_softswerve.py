"""
Cairn Test Case: Exhaustive Seattle soft serve ice cream research session.

This script simulates an agent using Cairn to iteratively discover all
soft serve shops in Seattle. It does NOT actually call search engines —
it mocks search results to test Cairn's session management, dedup,
page tracking, and coverage scoring logic end-to-end.

Run with: python test_seattle_softswerve.py
"""

from __future__ import annotations

import httpx
import json
import hashlib
import asyncio
from typing import Dict, List, Optional

BASE_URL = "http://localhost:8000"
API_KEY = "your-cairn-api-key"
HEADERS = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json"
}

# ─────────────────────────────────────────────────────────
# Mock search results — simulates what a real search engine
# would return for various queries about Seattle soft serve
# ─────────────────────────────────────────────────────────
MOCK_SEARCH_RESULTS = {
    "soft serve ice cream Seattle": [
        {"url": "https://www.softswerve.com/", "title": "Soft Swerve Seattle"},
        {"url": "https://www.yelp.com/biz/soft-swerve-seattle-2"},
        {"url": "https://mollymoon.com/locations", "title": "Molly Moon's"},
    ],
    "soft serve Seattle Capitol Hill": [
        {"url": "https://www.magicanncreamery.com/", "title": "Magic Ann Creamery"},
        {"url": "https://saltandstraw.com/pages/seattle", "title": "Salt & Straw Seattle"},
    ],
    "soft serve Seattle Ballard": [
        {"url": "https://www.twoscoopsballard.com/", "title": "Two Scoops Ballard"},
        {"url": "https://yelp.com/biz/soft-serve-ballard-seattle"},
    ],
    "soft serve Seattle Fremont": [
        {"url": "https://www.fremontcreamery.com/", "title": "Fremont Creamery"},
    ],
    "soft serve ice cream Seattle hours menu": [
        {"url": "https://www.softswerve.com/menu"},
        {"url": "https://www.magicanncreamery.com/hours"},
    ],
    "soft serve Seattle neighborhoods": [
        {"url": "https://www.timeout.com/seattle/restaurants/best-ice-cream-seattle"},
        {"url": "https://seattle.eater.com/maps/best-ice-cream-soft-serve-seattle"},
    ],
    "soft serve White Center SoDo Seattle": [
        {"url": "https://www.yelp.com/search?find_desc=soft+serve&find_loc=White+Center+Seattle"},
    ],
}

# ─────────────────────────────────────────────────────────
# Mock page extraction — simulates what an agent would
# extract from visiting each page
# ─────────────────────────────────────────────────────────
MOCK_EXTRACTED = {
    "https://www.softswerve.com/": {
        "shop_name": "Soft Swerve",
        "neighborhood": "Capitol Hill",
        "address": "217 Pine St, Seattle WA 98101",
        "hours": "Mon–Sun 12pm–10pm",
        "signature_flavors": ["ube", "matcha", "taro"],
        "price_range": "$5–$9"
    },
    "https://mollymoon.com/locations": {
        "shop_name": "Molly Moon's",
        "neighborhood": "Multiple (Capitol Hill, Fremont, Wallingford)",
        "address": "Various",
        "hours": "12pm–11pm daily",
        "signature_flavors": ["salted caramel", "honey lavender"],
        "price_range": "$4–$7"
    },
    "https://www.magicanncreamery.com/": {
        "shop_name": "Magic Ann Creamery",
        "neighborhood": "Capitol Hill",
        "address": "405 15th Ave E",
        "hours": "2pm–10pm Tue–Sun",
        "signature_flavors": ["black sesame", "pandan"],
        "price_range": "$6–$8"
    },
    "https://saltandstraw.com/pages/seattle": {
        "shop_name": "Salt & Straw",
        "neighborhood": "Capitol Hill",
        "address": "714 E Pike St",
        "hours": "11am–11pm daily",
        "signature_flavors": ["sea salt with caramel ribbons", "seasonal specials"],
        "price_range": "$6–$10"
    },
    "https://www.twoscoopsballard.com/": {
        "shop_name": "Two Scoops",
        "neighborhood": "Ballard",
        "address": "5214 Ballard Ave NW",
        "hours": "1pm–9pm Wed–Mon",
        "signature_flavors": ["classic vanilla twist", "chocolate hazelnut"],
        "price_range": "$4–$6"
    },
    "https://www.fremontcreamery.com/": {
        "shop_name": "Fremont Creamery",
        "neighborhood": "Fremont",
        "address": "3512 Fremont Ave N",
        "hours": "12pm–9pm daily",
        "signature_flavors": ["vanilla bean", "strawberry basil"],
        "price_range": "$5–$7"
    },
    "https://www.timeout.com/seattle/restaurants/best-ice-cream-seattle": {
        "shop_name": None,  # editorial list page
        "mentions": ["Soft Swerve", "Molly Moon's", "Husky Deli", "Fainting Goat"],
        "note": "List article — extract individual shop mentions"
    },
}


async def run_test():
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0) as client:
        print("\n═══════════════════════════════════════")
        print("  CAIRN TEST: Seattle Soft Serve Hunt")
        print("═══════════════════════════════════════\n")

        # ─────────────────────────────────
        # 1. Create session
        # ─────────────────────────────────
        print("▸ Creating session...")
        resp = await client.post(f"{BASE_URL}/sessions", json={
            "goal_prompt": "Find all soft serve ice cream shops in Seattle, WA. "
                          "Identify as many distinct shops as possible across all neighborhoods.",
            "goal_schema": {
                "dimensions": [
                    "shop_name",
                    "neighborhood",
                    "address",
                    "hours",
                    "signature_flavors",
                    "price_range"
                ]
            }
        })
        resp.raise_for_status()
        session = resp.json()
        session_id = session["id"]
        print(f"  Session ID: {session_id}\n")

        # ─────────────────────────────────
        # 2. Research loop
        # ─────────────────────────────────
        iteration = 0
        recommendation = "continue"

        while recommendation in ("continue", "diminishing") and iteration < 15:
            iteration += 1
            print(f"── Iteration {iteration} ─────────────────────────")

            # Get next query
            resp = await client.get(f"{BASE_URL}/sessions/{session_id}/next-query")
            resp.raise_for_status()
            next_q = resp.json()

            if next_q["phrase"] is None:
                print("  No new query angles. Stopping loop.")
                break

            phrase = next_q["phrase"]
            print(f"  Next query: \"{phrase}\"")
            print(f"  Reasoning: {next_q['reasoning']}")

            # Simulate search — find closest mock result
            mock_results = _find_mock_results(phrase)
            print(f"  Mock results: {len(mock_results)} pages found")

            # Log executed query
            resp = await client.post(
                f"{BASE_URL}/sessions/{session_id}/queries",
                json={"phrase": phrase, "result_count": len(mock_results)}
            )
            resp.raise_for_status()

            # Register pages
            if mock_results:
                resp = await client.post(
                    f"{BASE_URL}/sessions/{session_id}/pages",
                    json={"pages": [{"url": r["url"]} for r in mock_results]}
                )
                resp.raise_for_status()
                registered = resp.json()
                print(f"  Pages registered: {registered['registered']} "
                      f"(skipped {registered['skipped_duplicates']} duplicates)")

            # Visit queued pages
            resp = await client.get(
                f"{BASE_URL}/sessions/{session_id}/pages",
                params={"status": "queued"}
            )
            queued_pages = resp.json()["pages"]

            for page in queued_pages[:3]:  # visit up to 3 per iteration
                url = page["canonical_url"]
                extracted = MOCK_EXTRACTED.get(url, {})
                content_hash = hashlib.sha256(url.encode()).hexdigest()

                await client.patch(
                    f"{BASE_URL}/sessions/{session_id}/pages",
                    json={
                        "url": url,
                        "status": "visited",
                        "content_hash": content_hash,
                        "extracted": extracted
                    }
                )
                print(f"  Visited: {url}")

            # Get coverage every 2 iterations
            if iteration % 2 == 0:
                resp = await client.get(
                    f"{BASE_URL}/sessions/{session_id}/coverage",
                    params={"recompute": True}
                )
                resp.raise_for_status()
                coverage = resp.json()
                print(f"\n  📊 Coverage Score: {coverage['overall_score']:.0%}")
                for dim, data in coverage["dimensions"].items():
                    bar = "█" * int(data["score"] * 10) + "░" * (10 - int(data["score"] * 10))
                    print(f"    {dim:<20} {bar} {data['score']:.0%}  {data['note']}")
                if coverage.get("gaps"):
                    print(f"\n  Gaps: {'; '.join(coverage['gaps'][:2])}")
                print(f"\n  Marginal gain: {coverage.get('marginal_gain', 0):.3f}")
                print(f"  Recommendation: {coverage['recommendation'].upper()}")
                recommendation = coverage["recommendation"]

            print()

        # ─────────────────────────────────
        # 3. Final report
        # ─────────────────────────────────
        print("\n═══════════════════════════════════════")
        print("  FINAL RESULTS")
        print("═══════════════════════════════════════\n")

        resp = await client.get(f"{BASE_URL}/sessions/{session_id}")
        session_detail = resp.json()
        stats = session_detail["stats"]
        print(f"  Queries executed: {stats['queries_executed']}")
        print(f"  Pages visited:    {stats['pages_visited']}")
        print(f"  Iterations:       {iteration}")

        # List all visited pages with extracted shop data
        resp = await client.get(
            f"{BASE_URL}/sessions/{session_id}/pages",
            params={"status": "visited"}
        )
        pages = resp.json()["pages"]
        shops = [p["extracted"] for p in pages if p["extracted"].get("shop_name")]
        print(f"\n  Shops found: {len(shops)}\n")
        for shop in shops:
            print(f"  🍦 {shop.get('shop_name')}")
            print(f"     {shop.get('neighborhood')} — {shop.get('address')}")
            print(f"     Hours: {shop.get('hours', 'unknown')}")
            print(f"     Flavors: {', '.join(shop.get('signature_flavors', []))}")
            print(f"     Price: {shop.get('price_range', 'unknown')}\n")

        # Mark session complete
        await client.patch(
            f"{BASE_URL}/sessions/{session_id}",
            json={"status": "completed"}
        )
        print("  Session marked complete. ✓")


def _find_mock_results(phrase: str) -> List[Dict]:
    """Find closest matching mock results for a given phrase."""
    phrase_lower = phrase.lower()

    # Exact match first
    if phrase_lower in MOCK_SEARCH_RESULTS:
        return MOCK_SEARCH_RESULTS[phrase_lower]

    # Partial keyword match
    for key, results in MOCK_SEARCH_RESULTS.items():
        key_words = set(key.split())
        phrase_words = set(phrase_lower.split())
        if len(key_words & phrase_words) >= 2:
            return results

    # Fallback
    return MOCK_SEARCH_RESULTS.get("soft serve ice cream Seattle", [])


if __name__ == "__main__":
    asyncio.run(run_test())
