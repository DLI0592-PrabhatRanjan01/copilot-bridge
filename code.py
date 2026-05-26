"""
TakeUForward A2Z DSA Sheet - API Discovery & Scraping
======================================================
Uses requests to hit the backend API directly.
No Playwright/browser needed - works on any system with internet access.
"""

import json
import time
import re
import sys

try:
    import requests
except ImportError:
    print("[ERROR] requests library required: pip install requests")
    sys.exit(1)

API_BASE = "https://backend-go.takeuforward.org"
SITE_BASE = "https://takeuforward.org"
SHEET_URL = "https://takeuforward.org/strivers-a2z-dsa-course/strivers-a2z-dsa-course-sheet-2"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://takeuforward.org",
    "Referer": "https://takeuforward.org/",
}


def try_api_endpoints():
    print("\n[1/4] Discovering API endpoints...")
    endpoints = [
        "/api/v2/plus/subject/", "/api/v2/plus/subjects/", "/api/v2/plus/sheet/",
        "/api/v2/plus/sheets/", "/api/v2/plus/dsa/", "/api/v2/plus/course/",
        "/api/v2/plus/courses/", "/api/v2/subjects/", "/api/v2/sheets/",
        "/api/v1/subjects/", "/api/v1/sheets/",
        "/api/v2/plus/subject/a2z", "/api/v2/plus/subject/dsa",
        "/api/v2/plus/subject/strivers-a2z-dsa-course",
        "/api/v2/plus/sheet/a2z", "/api/v2/plus/sheet/strivers-a2z-dsa-course",
        "/api/v2/plus/sheet/strivers-a2z-dsa-course-sheet-2",
        "/api/v2/plus/dsa/a2z", "/api/v2/plus/dsa/problems", "/api/v2/plus/dsa/sheet",
        "/api/v2/plus/track/", "/api/v2/plus/tracks/",
        "/api/v2/plus/track/strivers-a2z-dsa-track",
        "/api/v2/plus/category/", "/api/v2/plus/categories/",
        "/api/v2/plus/problems/", "/api/v2/plus/problems/all",
        "/api/v2/problems/", "/api/v1/problems/",
        "/api/v2/plus/content/", "/api/v2/plus/content/dsa",
        "/api/v2/plus/landing/", "/api/v2/plus/page/", "/api/v2/plus/page/dsa",
        "/api/v2/plus/home/",
        "/api/v2/plus/problems?subjectSlug=strivers-a2z-dsa-track",
        "/api/v2/plus/problems?subjectSlug=a2z",
        "/api/v2/plus/subject?slug=strivers-a2z-dsa-track",
    ]
    working = []
    for endpoint in endpoints:
        url = API_BASE + endpoint
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    data_str = json.dumps(data)
                    if len(data_str) > 50:
                        print(f"  [200] {endpoint} -> {len(data_str)} bytes")
                        working.append({"endpoint": endpoint, "size": len(data_str),
                            "keys": list(data.keys()) if isinstance(data, dict) else f"list[{len(data)}]",
                            "preview": data_str[:500]})
                except:
                    if len(resp.text) > 100:
                        print(f"  [200] {endpoint} -> non-JSON ({len(resp.text)} bytes)")
            elif resp.status_code not in (404, 405):
                print(f"  [{resp.status_code}] {endpoint}")
        except:
            pass
    return working


def try_page_source():
    print("\n[2/4] Fetching page source for embedded data...")
    try:
        resp = requests.get(SHEET_URL, headers={"User-Agent": HEADERS["User-Agent"],
            "Accept": "text/html"}, timeout=30, allow_redirects=True)
        html = resp.text
        print(f"  Page size: {len(html)} bytes, URL: {resp.url}")
        
        api_urls = set(re.findall(r'(https?://[^"\'\s]+(?:api|backend)[^"\'\s]*)', html))
        if api_urls:
            print(f"  API URLs in source: {list(api_urls)[:15]}")
        
        next_data = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html)
        if next_data:
            print(f"  __NEXT_DATA__: {len(next_data.group(1))} bytes")
            try:
                nd = json.loads(next_data.group(1))
                print(f"  Keys: {list(nd.keys())}")
                return {"type": "next_data", "data": nd}
            except: pass
        
        slugs = re.findall(r'/plus/dsa/problems/([a-z0-9-]+)', html)
        if slugs:
            print(f"  Slugs in HTML: {len(set(slugs))}")
            return {"type": "slugs", "slugs": list(set(slugs))}
        
        fetch_calls = re.findall(r'fetch\(["\']([^"\']+)["\']', html)
        if fetch_calls:
            print(f"  Fetch calls: {fetch_calls[:10]}")
        
        chunks = re.findall(r'(/_next/static/chunks/[^"\']+\.js)', html)
        print(f"  JS chunks: {len(chunks)}")
        return {"type": "html", "size": len(html), "api_urls": list(api_urls)[:20]}
    except Exception as e:
        print(f"  Error: {e}")
        return {"type": "error", "error": str(e)}


def try_known_problem_api():
    print("\n[3/4] Testing problem metadata API...")
    test_slugs = ["set-matrix-zeroes", "pascals-triangle", "next-permutation",
        "kadanes-algorithm", "sort-an-array-of-0s-1s-and-2s", "two-sum",
        "majority-element", "longest-subarray-with-given-sum",
        "merge-overlapping-sub-intervals", "reverse-a-linked-list"]
    results = []
    for slug in test_slugs:
        url = f"{API_BASE}/api/v2/plus/problem/{slug}?subjectSlug="
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if "data" in data and data["data"]:
                    d = data["data"]
                    results.append({"slug": slug, "problem_name": d.get("problem_name"),
                        "difficulty": d.get("difficulty"), "lc_link": d.get("lc_link"),
                        "yt_link": d.get("yt_link"), "gfg_link": d.get("gfg_link"),
                        "cs_link": d.get("cs_link"), "problem_type": d.get("problem_type"),
                        "all_keys": list(d.keys())})
                    print(f"  [OK] {slug}: {d.get('problem_name')} | LC:{bool(d.get('lc_link'))} YT:{bool(d.get('yt_link'))}")
                else:
                    print(f"  [EMPTY] {slug}")
            else:
                print(f"  [{resp.status_code}] {slug}")
        except Exception as e:
            print(f"  [ERR] {slug}: {e}")
    print(f"\n  Working: {len(results)}/{len(test_slugs)}")
    return results


def try_sheet_api_variations():
    print("\n[4/4] Trying sheet-specific API variations...")
    variations = [
        f"{API_BASE}/api/v2/plus/subject/strivers-a2z-dsa-track",
        f"{API_BASE}/api/v2/plus/subject/strivers-a2z-dsa-track/categories",
        f"{API_BASE}/api/v2/plus/subject/strivers-a2z-dsa-track/problems",
        f"{API_BASE}/api/v2/plus/subject/dsa-a-to-z",
        f"{API_BASE}/api/v2/plus/sheet/strivers-a2z-dsa-track",
        f"{SITE_BASE}/sitemap.xml",
        f"{SITE_BASE}/sitemap-0.xml",
        f"{API_BASE}/api/v2/plus/topic/",
        f"{API_BASE}/api/v2/plus/topic/array",
        f"{API_BASE}/api/v2/plus/topics/",
        f"{API_BASE}/api/v2/plus/module/",
        f"{API_BASE}/api/v2/plus/modules/",
        f"{API_BASE}/api/v2/striver/",
        f"{API_BASE}/api/v2/striver/sheet",
        f"{API_BASE}/api/v1/plus/subject/",
    ]
    results = []
    for url in variations:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200 and len(resp.text) > 100:
                print(f"  [200] {url} ({len(resp.text)} bytes)")
                ct = resp.headers.get("Content-Type", "")
                if "json" in ct:
                    try:
                        data = resp.json()
                        if isinstance(data, dict):
                            print(f"       Keys: {list(data.keys())}")
                        elif isinstance(data, list):
                            print(f"       List[{len(data)}]")
                    except: pass
                results.append({"url": url, "size": len(resp.text), "preview": resp.text[:1000]})
            elif resp.status_code not in (404, 405, 403):
                print(f"  [{resp.status_code}] {url}")
        except: pass
    return results


def main():
    print("=" * 70)
    print("  TakeUForward A2Z DSA - API Discovery")
    print("=" * 70)
    print(f"  Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    all_results = {}
    all_results["api_endpoints"] = try_api_endpoints()
    all_results["page_source"] = try_page_source()
    all_results["problem_api_test"] = try_known_problem_api()
    all_results["sheet_api_variations"] = try_sheet_api_variations()
    
    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    print(f"  Working API endpoints: {len(all_results['api_endpoints'])}")
    print(f"  Problem API results: {len(all_results['problem_api_test'])}")
    print(f"  Sheet API results: {len(all_results['sheet_api_variations'])}")
    has_links = any(p.get("lc_link") or p.get("yt_link") for p in all_results["problem_api_test"])
    print(f"  Problem API has links: {has_links}")
    
    with open("api_discovery_results.json", "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  Saved: api_discovery_results.json ({len(json.dumps(all_results, default=str))//1024} KB)")

if __name__ == "__main__":
    main()
