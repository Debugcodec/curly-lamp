import os
import re
import time
import json
import uuid
import sqlite3
import subprocess
import html as html_lib
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, quote_plus
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, render_template_string
from bs4 import BeautifulSoup
from curl_cffi import requests
from PIL import Image

# ==========================================
# CONFIGURATION
# ==========================================
DB_PATH = os.path.join(os.path.expanduser("~"), "reviews_cache.db")

SORT_PARAMS = {
    "recent": "MOST_RECENT",
    "helpful": "MOST_HELPFUL",
    "positive": "POSITIVE_FIRST",
    "negative": "NEGATIVE_FIRST"
}

app = Flask(__name__)

# ==========================================
# DATE NORMALIZER FOR RECENT SORTING
# ==========================================
def parse_relative_date_to_timestamp(date_str: str) -> int:
    if not date_str:
        return 0
    
    s = date_str.strip().lower()
    now = datetime.now()

    if "today" in s or "just now" in s:
        return int(now.timestamp())
    if "yesterday" in s:
        return int((now - timedelta(days=1)).timestamp())

    match_rel = re.search(r"(\d+)\s+(day|days|month|months|year|years|hour|hours|min|mins)\s+ago", s)
    if match_rel:
        val = int(match_rel.group(1))
        unit = match_rel.group(2)
        if "min" in unit:
            return int((now - timedelta(minutes=val)).timestamp())
        elif "hour" in unit:
            return int((now - timedelta(hours=val)).timestamp())
        elif "day" in unit:
            return int((now - timedelta(days=val)).timestamp())
        elif "month" in unit:
            return int((now - timedelta(days=val * 30)).timestamp())
        elif "year" in unit:
            return int((now - timedelta(days=val * 365)).timestamp())

    cleaned_date = re.sub(r"[^\w\s]", " ", date_str).strip()
    for fmt in ("%b %Y", "%B %Y", "%d %b %Y", "%d %B %Y", "%Y %m %d"):
        try:
            return int(datetime.strptime(cleaned_date, fmt).timestamp())
        except ValueError:
            pass

    return 0

# ==========================================
# DATABASE LAYER (SQLITE)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                review_id TEXT PRIMARY KEY,
                pid TEXT,
                product_title TEXT,
                rating TEXT,
                title TEXT,
                text TEXT,
                author TEXT,
                location TEXT,
                date TEXT,
                post_timestamp INTEGER DEFAULT 0,
                link TEXT,
                sort_tag TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("PRAGMA table_info(reviews)")
        cols = [c[1] for c in cursor.fetchall()]
        if "post_timestamp" not in cols:
            cursor.execute("ALTER TABLE reviews ADD COLUMN post_timestamp INTEGER DEFAULT 0")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pid ON reviews(pid)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_author ON reviews(author COLLATE NOCASE)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON reviews(post_timestamp)")
        conn.commit()
    finally:
        conn.close()

def clear_all_reviews():
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=20)
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM reviews")
        cursor.execute("VACUUM")
    finally:
        conn.close()

def save_reviews_to_db(pid: str, product_title: str, reviews: list, sort_tag: str = "recent"):
    conn = sqlite3.connect(DB_PATH, timeout=20)
    try:
        cursor = conn.cursor()
        for r in reviews:
            ts = r.get("post_timestamp") or parse_relative_date_to_timestamp(r.get("date", ""))
            cursor.execute("""
                INSERT OR REPLACE INTO reviews 
                (review_id, pid, product_title, rating, title, text, author, location, date, post_timestamp, link, sort_tag)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                r.get("reviewId") or r.get("review_id"),
                pid, product_title,
                r.get("rating"), r.get("title"), r.get("text"),
                r.get("author"), r.get("location"), r.get("date"),
                ts,
                r.get("link"), sort_tag
            ))
        conn.commit()
    finally:
        conn.close()

def get_order_clause(sort_type: str) -> str:
    clean_num = "CAST(REPLACE(REPLACE(rating, '★', ''), ' ', '') AS FLOAT)"
    if sort_type == "negative":
        return f"{clean_num} ASC, post_timestamp DESC, rowid DESC"
    elif sort_type == "positive":
        return f"{clean_num} DESC, post_timestamp DESC, rowid DESC"
    elif sort_type == "helpful":
        return "CASE WHEN sort_tag = 'helpful' THEN 0 ELSE 1 END, post_timestamp DESC, rowid DESC"
    return "post_timestamp DESC, rowid DESC"

def get_cached_reviews(pid: str, limit: int = 10, sort_type: str = "recent"):
    conn = sqlite3.connect(DB_PATH, timeout=20)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        order_clause = get_order_clause(sort_type)
        query = f"""
            SELECT rowid AS id, * FROM reviews 
            WHERE pid = ? 
            ORDER BY {order_clause} 
            LIMIT ?
        """
        cursor.execute(query, (pid, limit))
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()

def count_cached_reviews(pid: str = None) -> int:
    conn = sqlite3.connect(DB_PATH, timeout=20)
    try:
        cursor = conn.cursor()
        if pid:
            cursor.execute("SELECT COUNT(*) FROM reviews WHERE pid = ?", (pid,))
        else:
            cursor.execute("SELECT COUNT(*) FROM reviews")
        res = cursor.fetchone()
        return res[0] if res else 0
    finally:
        conn.close()

def search_reviews_in_db(pid: str, query_str: str, sort_type: str = "recent", limit: int = 10):
    q = f"%{query_str.strip()}%"
    conn = sqlite3.connect(DB_PATH, timeout=20)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        order_clause = get_order_clause(sort_type)
        query = f"""
            SELECT rowid AS id, * FROM reviews 
            WHERE pid = ? AND (author LIKE ? OR text LIKE ? OR title LIKE ? OR location LIKE ?)
            ORDER BY {order_clause}
            LIMIT ?
        """
        cursor.execute(query, (pid, q, q, q, q, limit))
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()

# ==========================================
# SCRAPER CORE
# ==========================================
def clean_location(loc_obj) -> str:
    if not loc_obj:
        return ""
    if isinstance(loc_obj, dict):
        city = loc_obj.get("city") or loc_obj.get("cityName") or ""
        state = loc_obj.get("state") or loc_obj.get("stateName") or ""
        parts = [p.strip() for p in [city, state] if p and str(p).strip()]
        return ", ".join(parts)
    if isinstance(loc_obj, str):
        return loc_obj.strip()
    return ""

def clean_date_string(raw_date: str) -> str:
    if not raw_date:
        return ""
    d = str(raw_date).strip()
    d = re.sub(r"^Verified Purchase\s*[,•·]?\s*", "", d, flags=re.IGNORECASE)
    d = re.sub(r"^Certified Buyer\s*[,•·]?\s*", "", d, flags=re.IGNORECASE)
    return d.strip()

def extract_author_name(node) -> str:
    if not node:
        return "Flipkart Customer"
    if isinstance(node, dict):
        name = node.get("name") or node.get("authorName") or node.get("reviewerName") or node.get("text")
        if name and isinstance(name, str) and name.strip():
            return html_lib.unescape(name.strip())
    if isinstance(node, str) and node.strip():
        if not node.startswith("{") and "Location" not in node:
            return html_lib.unescape(node.strip())
    return "Flipkart Customer"

def fetch_flipkart_reviews(pid: str, page: int = 1, sort_type: str = "recent", original_url: str = ""):
    sort_param = SORT_PARAMS.get(sort_type, "MOST_RECENT")
    url = f"https://www.flipkart.com/product/product-reviews/item?pid={pid}&sortOrder={sort_param}&page={page}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.flipkart.com/product/product-reviews/item?pid={pid}"
    }

    try:
        response = requests.get(url, headers=headers, impersonate="chrome120", timeout=8)
    except Exception as e:
        return None, f"Request error: {e}", ""

    if response.status_code != 200:
        return None, f"Status: {response.status_code}", ""

    html_text = response.text
    product_title = f"Product ({pid})"
    match_title = re.search(r'"productTitle"\s*:\s*"([^"]+)"|"title"\s*:\s*"([^"]+)"', html_text)
    if match_title:
        cand = match_title.group(1) or match_title.group(2)
        if cand and len(cand) > 4:
            product_title = html_lib.unescape(cand).strip()

    reviews_found = []
    seen_ids = set()
    soup = BeautifulSoup(html_text, "html.parser")
    card_containers = soup.find_all("div", class_=re.compile(r"cPHDOP|col-12-12|_27M-vq|col _2w2M|row _200c3e|_1AtVbE|EKF0dM"))

    for card in card_containers:
        card_html = str(card)
        r_id_match = re.search(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", card_html)
        if not r_id_match:
            continue

        r_id = r_id_match.group(1)
        if r_id in seen_ids:
            continue

        link_elem = card.find("a", href=re.compile(r"/reviews/|\?reviewId=", re.IGNORECASE))
        if link_elem and "href" in link_elem.attrs:
            raw_href = link_elem["href"]
            review_link = f"https://www.flipkart.com{raw_href}" if not raw_href.startswith("http") else raw_href
        else:
            review_link = f"https://www.flipkart.com/reviews/{pid}:{len(seen_ids)+1}?reviewId={r_id}"

        rating_elem = card.find("div", class_=re.compile(r"XDXgAE|_3LWZlK|_1BLPMq"))
        title_elem = card.find("p", class_=re.compile(r"z9E0IG|_2-N8zT"))
        text_elem = card.find("div", class_=re.compile(r"ZmyHeo|_11XBLa|txt|row _3nLaff"))
        author_elem = card.find("p", class_=re.compile(r"_2NsDsF|_2sc7ZR"))
        date_elem = card.find("p", class_=re.compile(r"_3c7q|wX4Z|M4Vp|_2mcPpE|_2_R_DZ"))

        post_date = ""
        if date_elem:
            post_date = clean_date_string(date_elem.get_text(strip=True))
        else:
            time_match = re.search(r"(\d+\s+(?:day|days|month|months|year|years|hour|hours|min|mins)\s+ago|Today|Yesterday)", card.get_text())
            if time_match:
                post_date = time_match.group(1)

        author_str = "Flipkart Customer"
        loc_str = ""
        if author_elem:
            raw_text = author_elem.get_text(strip=True)
            if "," in raw_text:
                parts = raw_text.split(",", 1)
                author_str = parts[0].strip()
                loc_str = parts[1].strip()
            else:
                author_str = raw_text

        raw_rating = rating_elem.get_text(strip=True) if rating_elem else "5"
        clean_rate = re.findall(r"\d+", raw_rating)
        rating_val = clean_rate[0] if clean_rate else "5"

        seen_ids.add(r_id)
        reviews_found.append({
            "reviewId": r_id,
            "rating": rating_val,
            "title": html_lib.unescape(title_elem.get_text(strip=True)) if title_elem else "Highly recommended",
            "text": html_lib.unescape(text_elem.get_text(separator=" ", strip=True)) if text_elem else "No review content provided.",
            "author": html_lib.unescape(author_str),
            "location": loc_str or "India",
            "date": post_date or "Recently",
            "post_timestamp": parse_relative_date_to_timestamp(post_date),
            "link": review_link,
        })

    def walk_json(node):
        if isinstance(node, dict):
            if ("reviewId" in node or "id" in node) and any(k in node for k in ["rating", "author"]):
                r_id = node.get("reviewId") or node.get("id")
                if isinstance(r_id, str) and re.match(r"^[a-f0-9\-]{36}$", r_id) and r_id not in seen_ids:
                    seen_ids.add(r_id)
                    raw_url = node.get("url") or f"/reviews/{pid}:1?reviewId={r_id}"
                    full_link = f"https://www.flipkart.com{raw_url}" if not raw_url.startswith("http") else raw_url
                    
                    r_str = str(node.get("rating") or node.get("starRating", "5"))
                    c_rate = re.findall(r"\d+", r_str)
                    clean_score = c_rate[0] if c_rate else "5"
                    raw_dt = clean_date_string(node.get("created") or node.get("reviewDate") or "")

                    reviews_found.append({
                        "reviewId": r_id,
                        "rating": clean_score,
                        "title": html_lib.unescape(str(node.get("title") or "Recommended")),
                        "text": html_lib.unescape(str(node.get("text") or node.get("reviewText", ""))),
                        "author": extract_author_name(node.get("author")),
                        "location": clean_location(node.get("location")) or "India",
                        "date": raw_dt or "Recently",
                        "post_timestamp": parse_relative_date_to_timestamp(raw_dt),
                        "link": full_link
                    })
            for v in node.values():
                walk_json(v)
        elif isinstance(node, list):
            for item in node:
                walk_json(item)

    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html_text, flags=re.DOTALL)
    for script in scripts:
        if "reviewId" in script:
            for raw in re.findall(r"=\s*(\{.*?\})\s*;", script, re.DOTALL):
                try:
                    walk_json(json.loads(raw))
                except Exception:
                    pass

    return reviews_found, pid, product_title

# ==========================================
# MULTI-FILTER DEEP CRAWLER
# ==========================================
def crawl_pages(pid: str, requested_pages: int = 1, sort_type: str = "recent", original_url: str = ""):
    all_revs = []
    final_title = f"Product ({pid})"
    seen_ids = set()

    conn = sqlite3.connect(DB_PATH, timeout=20)
    try:
        c = conn.cursor()
        c.execute("SELECT review_id FROM reviews WHERE pid = ?", (pid,))
        for row in c.fetchall():
            seen_ids.add(row[0])
    finally:
        conn.close()

    if requested_pages <= 3:
        sort_list = [sort_type]
        per_sort_pages = requested_pages
    else:
        sort_list = ["recent", "helpful", "positive", "negative"] if requested_pages >= 10 else [sort_type]
        per_sort_pages = max(1, requested_pages // len(sort_list) if len(sort_list) > 1 else requested_pages)

    def fetch_page_worker(args):
        p, s_type = args
        revs, _, t = fetch_flipkart_reviews(pid, p, s_type, original_url)
        return revs or [], t

    tasks = []
    for s in sort_list:
        for page_num in range(1, per_sort_pages + 1):
            tasks.append((page_num, s))

    with ThreadPoolExecutor(max_workers=min(8, len(tasks) or 1)) as pool:
        results = pool.map(fetch_page_worker, tasks)
        for batch_revs, t in results:
            if t and "Product (" not in t:
                final_title = t
            for r in batch_revs:
                r_id = r.get("reviewId")
                if r_id and r_id not in seen_ids:
                    seen_ids.add(r_id)
                    all_revs.append(r)

    if all_revs:
        save_reviews_to_db(pid, final_title, all_revs, sort_type)

    return all_revs, final_title

# ==========================================
# UI TEMPLATE
# ==========================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>ReviewLens</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #f1f5f9;
            --surface: #ffffff;
            --primary: #4f46e5;
            --text-title: #0f172a;
            --text-body: #334155;
            --text-muted: #64748b;
            --border: #e2e8f0;
            --success: #10b981;
            --danger: #ef4444;
            --card-shadow: 0 10px 25px -5px rgba(15, 23, 42, 0.05), 0 8px 10px -6px rgba(15, 23, 42, 0.03);
            --inner-shadow: inset 0 2px 4px 0 rgba(0, 0, 0, 0.02);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
            -webkit-tap-highlight-color: transparent;
        }

        body {
            background-color: var(--bg);
            color: var(--text-body);
            padding: 24px 16px;
            display: flex;
            justify-content: center;
            min-height: 100vh;
        }

        .container {
            width: 100%;
            max-width: 480px;
            display: flex;
            flex-direction: column;
            gap: 20px;
        }

        .app-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 4px 8px;
        }

        .app-brand {
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .brand-icon {
            width: 38px;
            height: 38px;
            background: linear-gradient(135deg, #4f46e5, #818cf8);
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #ffffff;
            font-size: 1.2rem;
            box-shadow: 0 4px 12px rgba(79, 70, 229, 0.3);
        }

        .brand-text h1 {
            font-size: 1.15rem;
            font-weight: 800;
            color: var(--text-title);
            letter-spacing: -0.02em;
        }

        .brand-text p {
            font-size: 0.75rem;
            color: var(--text-muted);
            font-weight: 500;
        }

        .btn-clear-db {
            background: #fee2e2;
            border: 1px solid #fecaca;
            color: #b91c1c;
            padding: 7px 12px;
            border-radius: 10px;
            font-size: 0.75rem;
            font-weight: 700;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            transition: all 0.2s ease;
        }

        .btn-clear-db:active {
            background: #fecaca;
            transform: scale(0.96);
        }

        .control-panel {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 20px;
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 16px;
            box-shadow: var(--card-shadow);
        }

        .input-group {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .label-row {
            display: flex;
            justify-content: space-between;
            align-items: baseline;
        }

        .label-row label {
            font-size: 0.85rem;
            font-weight: 700;
            color: var(--text-title);
            letter-spacing: -0.01em;
        }

        .label-hint {
            font-size: 0.72rem;
            color: var(--text-muted);
            font-weight: 600;
        }

        .input-field, .select-field {
            width: 100%;
            background: #f8fafc;
            border: 1.5px solid var(--border);
            border-radius: 14px;
            padding: 12px 14px;
            color: var(--text-title);
            font-size: 0.92rem;
            font-weight: 500;
            outline: none;
            transition: all 0.2s ease;
            box-shadow: var(--inner-shadow);
        }

        .input-field:focus, .select-field:focus {
            border-color: var(--primary);
            background: #ffffff;
            box-shadow: 0 0 0 4px rgba(79, 70, 229, 0.12);
        }

        .row-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }

        .btn-primary {
            background: linear-gradient(135deg, var(--primary), #6366f1);
            color: #ffffff;
            border: none;
            border-radius: 14px;
            padding: 14px;
            font-size: 0.98rem;
            font-weight: 700;
            cursor: pointer;
            box-shadow: 0 6px 20px -2px rgba(79, 70, 229, 0.4);
            transition: all 0.2s ease;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
        }

        .btn-primary:active {
            transform: scale(0.98);
            box-shadow: 0 3px 10px rgba(79, 70, 229, 0.3);
        }

        .analytics-deck {
            display: none;
            flex-direction: column;
            gap: 14px;
        }

        .stat-banner {
            background: linear-gradient(135deg, #1e1b4b, #312e81);
            color: #ffffff;
            border-radius: 20px;
            padding: 18px 22px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            box-shadow: 0 10px 25px -3px rgba(30, 27, 75, 0.25);
        }

        .stat-info span {
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: #a5b4fc;
            font-weight: 600;
        }

        .stat-info h2 {
            font-size: 2rem;
            font-weight: 800;
            line-height: 1.1;
            margin-top: 2px;
        }

        .stat-badge {
            background: rgba(255, 255, 255, 0.12);
            border: 1px solid rgba(255, 255, 255, 0.2);
            padding: 6px 12px;
            border-radius: 12px;
            font-size: 0.8rem;
            font-weight: 600;
            backdrop-filter: blur(8px);
        }

        .search-shell {
            position: relative;
            display: flex;
            align-items: center;
        }

        .search-shell svg {
            position: absolute;
            left: 16px;
            width: 18px;
            height: 18px;
            color: var(--text-muted);
            pointer-events: none;
        }

        .search-input {
            width: 100%;
            background: var(--surface);
            border: 1.5px solid var(--border);
            border-radius: 16px;
            padding: 14px 16px 14px 44px;
            font-size: 0.92rem;
            font-weight: 500;
            color: var(--text-title);
            outline: none;
            box-shadow: var(--card-shadow);
            transition: all 0.2s ease;
        }

        .search-input:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 4px rgba(79, 70, 229, 0.12);
        }

        .reviews-stream {
            display: flex;
            flex-direction: column;
            gap: 14px;
        }

        .review-card {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 18px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            box-shadow: var(--card-shadow);
            transition: transform 0.2s ease;
        }

        .card-head {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .score-pill {
            background: #ecfdf5;
            color: #047857;
            border: 1px solid #a7f3d0;
            padding: 4px 10px;
            border-radius: 10px;
            font-size: 0.82rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 4px;
        }

        .score-pill.score-low {
            background: #fff1f2;
            color: #be123c;
            border-color: #fecdd3;
        }

        .btn-clipboard {
            background: #f8fafc;
            border: 1px solid var(--border);
            color: var(--text-body);
            border-radius: 10px;
            padding: 6px 12px;
            font-size: 0.78rem;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            transition: all 0.15s ease;
        }

        .btn-clipboard:active {
            background: #e2e8f0;
            transform: scale(0.96);
        }

        .review-heading {
            font-size: 1rem;
            font-weight: 700;
            color: var(--text-title);
            line-height: 1.35;
        }

        .review-body {
            font-size: 0.88rem;
            color: var(--text-body);
            line-height: 1.55;
            font-weight: 450;
        }

        .card-footer {
            border-top: 1px solid #f1f5f9;
            padding-top: 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.78rem;
        }

        .author-box {
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .author-name {
            font-weight: 700;
            color: var(--text-title);
        }

        .verify-tag {
            color: var(--success);
            display: inline-flex;
            align-items: center;
            font-size: 0.9rem;
        }

        .meta-box {
            color: var(--text-muted);
            font-weight: 500;
        }

        .toast-banner {
            position: fixed;
            bottom: 24px;
            background: #0f172a;
            color: #ffffff;
            padding: 12px 22px;
            border-radius: 30px;
            font-size: 0.85rem;
            font-weight: 600;
            box-shadow: 0 10px 25px rgba(0,0,0,0.2);
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.25s ease;
            z-index: 1000;
        }
    </style>
</head>
<body>

<div class="container">
    <header class="app-header">
        <div class="app-brand">
            <div class="brand-icon">⚡</div>
            <div class="brand-text">
                <h1>ReviewLens</h1>
                <p>Flipkart Deep Indexer & Search</p>
            </div>
        </div>
        <button class="btn-clear-db" onclick="clearDatabase()">
            <span>🗑️ Clear Database</span>
        </button>
    </header>

    <section class="control-panel">
        <div class="input-group">
            <div class="label-row">
                <label>Flipkart / Shopsy URL</label>
            </div>
            <input type="text" id="productUrl" class="input-field" placeholder="Paste item web link here...">
        </div>

        <div class="row-grid">
            <div class="input-group">
                <div class="label-row">
                    <label>Sort By</label>
                </div>
                <select id="sortOption" class="select-field" onchange="handleSortChange()">
                    <option value="recent">Most Recent</option>
                    <option value="helpful">Most Helpful</option>
                    <option value="positive">Positive First</option>
                    <option value="negative">Negative First</option>
                </select>
            </div>

            <div class="input-group">
                <div class="label-row">
                    <label>Scan Depth</label>
                    <span class="label-hint" id="targetCountHint">10 reviews</span>
                </div>
                <input type="number" id="pageCount" class="input-field" value="1" min="1" max="200" oninput="handleScanDepthChange()">
            </div>
        </div>

        <button class="btn-primary" id="getReviewsBtn" onclick="fetchReviews(true)">
            <span>Fetch Reviews</span>
        </button>
    </section>

    <section class="analytics-deck" id="analyticsDeck">
        <div class="stat-banner">
            <div class="stat-info">
                <span>Reviews Shown</span>
                <h2 id="statsCount">0</h2>
            </div>
            <div class="stat-badge" id="productBadge">Active</div>
        </div>

        <div class="search-shell">
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            <input type="text" id="searchInput" class="search-input" placeholder="Search by name, state, keyword..." oninput="handleSearch()">
        </div>
    </section>

    <main class="reviews-stream" id="reviewsList"></main>
</div>

<div class="toast-banner" id="toast">Link copied!</div>

<script>
let currentPid = "";
let debounceTimer = null;

function extractPid(url) {
    let match = url.match(/[?&]pid=([A-Z0-9]+)/i);
    if (match) return match[1];
    let matchAlt = url.match(/\\/([A-Z0-9]{16})(?:[?&\\/]|$)/i);
    return matchAlt ? matchAlt[1] : null;
}

function showToast(msg) {
    const toast = document.getElementById("toast");
    toast.innerText = msg;
    toast.style.opacity = "1";
    setTimeout(() => { toast.style.opacity = "0"; }, 2200);
}

async function clearDatabase() {
    if (!confirm("Are you sure you want to clear all reviews stored in SQLite?")) {
        return;
    }

    try {
        const res = await fetch('/api/clear_db', { method: 'POST' });
        const data = await res.json();
        if (data.status === "ok") {
            currentPid = "";
            document.getElementById("statsCount").innerText = "0";
            document.getElementById("analyticsDeck").style.display = "none";
            document.getElementById("reviewsList").innerHTML = "";
            document.getElementById("searchInput").value = "";
            showToast("Database successfully cleared!");
        } else {
            showToast("❌ " + (data.message || "Failed to clear database"));
        }
    } catch(e) {
        showToast("❌ Error clearing database");
    }
}

function copyToClipboard(btn) {
    const link = btn.getAttribute("data-link");
    if (!link) return;

    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(link).then(() => {
            showToast("Review link copied!");
        }).catch(() => fallbackCopy(link));
    } else {
        fallbackCopy(link);
    }
}

function fallbackCopy(text) {
    const t = document.createElement("textarea");
    t.value = text;
    t.style.position = "fixed";
    t.style.left = "-9999px";
    document.body.appendChild(t);
    t.focus();
    t.select();
    try {
        document.execCommand("copy");
        showToast("Review link copied!");
    } catch(e) {
        showToast("Copy failed");
    } finally {
        document.body.removeChild(t);
    }
}

async function fetchReviews(forceRefresh = true) {
    const url = document.getElementById("productUrl").value.trim();
    const sort = document.getElementById("sortOption").value;
    const pages = parseInt(document.getElementById("pageCount").value, 10) || 1;
    const btn = document.getElementById("getReviewsBtn");

    const pid = extractPid(url);
    if (!pid) {
        alert("Enter a valid Flipkart link with PID.");
        return;
    }

    currentPid = pid;
    btn.innerHTML = `<span>Fetching...</span>`;
    btn.disabled = true;

    try {
        const fetchUrl = `/api/fetch?pid=${pid}&pages=${pages}&sort=${sort}&url=${encodeURIComponent(url)}&refresh=${forceRefresh ? '1' : '0'}`;
        const res = await fetch(fetchUrl);
        const data = await res.json();
        
        document.getElementById("analyticsDeck").style.display = "flex";
        document.getElementById("statsCount").innerText = data.reviews.length;

        const query = document.getElementById("searchInput").value.trim();
        if (query) {
            handleSearch();
        } else {
            renderCards(data.reviews);
        }
    } catch(e) {
        alert("Error fetching reviews.");
    } finally {
        btn.innerHTML = `<span>Fetch Reviews</span>`;
        btn.disabled = false;
    }
}

function handleScanDepthChange() {
    const val = parseInt(document.getElementById("pageCount").value, 10);
    const hint = document.getElementById("targetCountHint");
    if (!isNaN(val) && val > 0) {
        hint.innerText = (val * 10) + " reviews";
    }

    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
        const url = document.getElementById("productUrl").value.trim();
        const pid = extractPid(url);
        if (pid) {
            fetchReviews(true);
        }
    }, 450);
}

async function handleSortChange() {
    if (!currentPid) return;
    const query = document.getElementById("searchInput").value.trim();
    if (query) {
        handleSearch();
    } else {
        const sort = document.getElementById("sortOption").value;
        const pages = parseInt(document.getElementById("pageCount").value, 10) || 1;
        const res = await fetch(`/api/fetch?pid=${currentPid}&pages=${pages}&sort=${sort}&refresh=0`);
        const data = await res.json();
        document.getElementById("statsCount").innerText = data.reviews.length;
        renderCards(data.reviews);
    }
}

async function handleSearch() {
    if (!currentPid) return;
    const query = document.getElementById("searchInput").value.trim();
    const sort = document.getElementById("sortOption").value;
    const pages = parseInt(document.getElementById("pageCount").value, 10) || 1;

    if (!query) {
        const res = await fetch(`/api/fetch?pid=${currentPid}&pages=${pages}&sort=${sort}&refresh=0`);
        const data = await res.json();
        document.getElementById("statsCount").innerText = data.reviews.length;
        renderCards(data.reviews);
        return;
    }

    const res = await fetch(`/api/search?pid=${currentPid}&q=${encodeURIComponent(query)}&sort=${sort}&pages=${pages}`);
    const data = await res.json();
    document.getElementById("statsCount").innerText = data.reviews.length;
    renderCards(data.reviews);
}

function renderCards(reviews) {
    const list = document.getElementById("reviewsList");
    list.innerHTML = "";

    if (!reviews || reviews.length === 0) {
        list.innerHTML = "<div style='text-align:center; color:#94a3b8; padding:30px; font-size:0.9rem;'>No matching reviews found.</div>";
        return;
    }

    reviews.forEach(r => {
        const card = document.createElement("div");
        card.className = "review-card";
        const scoreNum = parseFloat(r.rating) || 5;
        const isLow = scoreNum <= 2;
        
        card.innerHTML = `
            <div class="card-head">
                <div class="score-pill ${isLow ? 'score-low' : ''}">
                    ★ ${r.rating}
                </div>
                <button class="btn-clipboard" data-link="${encodeURI(r.link)}" onclick="copyToClipboard(this)">
                    <span>Copy Link</span>
                </button>
            </div>

            <div class="review-heading">${r.title}</div>
            <div class="review-body">${r.text}</div>

            <div class="card-footer">
                <div class="author-box">
                    <span class="author-name">${r.author}</span>
                    <span class="verify-tag" title="Certified Buyer">✔</span>
                </div>
                <div class="meta-box">${r.location} · ${r.date}</div>
            </div>
        `;
        list.appendChild(card);
    });
}
</script>

</body>
</html>
"""

# ==========================================
# FLASK HTTP ROUTES
# ==========================================
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/fetch")
def api_fetch():
    pid = request.args.get("pid")
    pages = min(int(request.args.get("pages", 1)), 200)
    sort_type = request.args.get("sort", "recent")
    original_url = request.args.get("url", "")
    force_refresh = request.args.get("refresh", "1") == "1"

    if original_url and force_refresh:
        crawl_pages(pid, requested_pages=pages, sort_type=sort_type, original_url=original_url)

    target_limit = pages * 10
    reviews = get_cached_reviews(pid, limit=target_limit, sort_type=sort_type)
    total_count = count_cached_reviews(pid)

    return jsonify({
        "status": "ok",
        "reviews": reviews,
        "total_cached": total_count
    })

@app.route("/api/search")
def api_search():
    pid = request.args.get("pid")
    query = request.args.get("q", "")
    sort_type = request.args.get("sort", "recent")
    pages = min(int(request.args.get("pages", 1)), 200)
    target_limit = pages * 10
    
    results = search_reviews_in_db(pid, query, sort_type=sort_type, limit=target_limit)
    return jsonify({
        "status": "ok",
        "reviews": results
    })

@app.route("/api/clear_db", methods=["POST"])
def api_clear_db():
    try:
        clear_all_reviews()
        return jsonify({"status": "ok", "message": "Database wiped successfully"})
    except Exception as e:
        print(f"❌ Error clearing DB: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    init_db()
    print("🌐 Dashboard active on: http://127.0.0.1:8000")
    app.run(host="0.0.0.0", port=8000, debug=False)
