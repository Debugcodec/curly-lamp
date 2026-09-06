# ReviewLens ⚡

A lightweight, high-performance Flipkart and Shopsy review indexer, search engine, and dashboard built with Flask, SQLite, and `curl_cffi`. 

ReviewLens bypasses standard client limitations by multi-threading requests across multiple sort endpoints, storing reviews locally with normalized epoch timestamps, and providing instant full-text search with zero external database dependencies.

---

## Features

- **Multi-Filter Deep Crawling:** Concurrently queries across Recent, Helpful, Positive, and Negative review feeds using `ThreadPoolExecutor` to bypass single-sort pagination ceilings.
- **Accurate Chronological Sorting:** Normalizes relative Flipkart date strings (e.g., `"Today"`, `"2 days ago"`, `"Jan, 2025"`) into epoch timestamps for accurate sorting.
- **Dynamic Scan Depth:** Directly controls the target query limit (`1` = 10 reviews, `2` = 20 reviews, etc.).
- **Live Local Search:** Instant client-side and SQLite full-text search across author names, review titles, descriptions, and locations.
- **One-Click Clipboard Links:** Direct access links generated for every indexed review card.
- **Cache Management:** Built-in dashboard control to inspect cached reviews and wipe the database with safe SQLite WAL mode execution.

---

## Tech Stack

- **Backend:** Python 3, Flask
- **Scraping Engine:** `curl_cffi` (impersonating Chrome TLS fingerprints), BeautifulSoup4
- **Database:** SQLite3 (WAL mode enabled)
- **Frontend:** Responsive vanilla HTML5, CSS3, JavaScript (embedded single-file design)

---

## Installation & Setup

### 1. Clone the Repository

```bash
git clone [https://github.com/Debugcodec/curly-lamp.git](https://github.com/Debugcodec/curly-lamp.git)
cd curly-lamp

```
### 2. Install Dependencies

Ensure Python 3 and `pip` are installed:

```bash
pip install -r requirements.txt
```

### 3. Run the Application

```bash
python web.py
```
Open your browser and navigate to:
http://localhost:8000

(If running in Termux, access [http://127.0.0.1:8000](http://127.0.0.1:8000) on your mobile browser).
Usage
 * Fetch Reviews:
   * Copy any Flipkart or Shopsy product URL containing a pid parameter (e.g., [https://www.flipkart.com/.../p/itm](https://www.flipkart.com/.../p/itm)...).
   * Paste it into the Flipkart / Shopsy URL input field.
   * Set your desired Scan Depth (1 = 10 reviews, 5 = 50 reviews, etc.).
   * Choose your preferred sort criteria and tap Fetch Reviews.
 * Search:
   * Use the search bar to filter indexed reviews by reviewer name, state/city, or keywords in real time.
 * Copy Review Links:
   * Tap Copy Link on any card to copy the direct review URL to your clipboard.
 * Clear Cache:
   * Tap 🗑️ Clear Database in the upper right corner to wipe stored reviews and reclaim local storage.

## Project Structure

```text
curly-lamp/
├── .gitignore          # Excludes SQLite database and cache files from Git
├── LICENSE             # MIT License terms and distribution rights
├── README.md           # Documentation, setup guide, and usage instructions
├── requirements.txt    # Python package dependencies (Flask, curl_cffi, etc.)
└── web.py              # Main single-file Flask dashboard, scraper, and SQLite core
