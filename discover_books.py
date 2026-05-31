#!/usr/bin/env python3
"""
Discover high-rated non-fiction audiobooks on OverDrive (Libby) and/or BorrowBox,
filtered by Goodreads ratings, excluding already-read books.

Uses local caches to track previously checked books per library.
Only new titles get Goodreads lookups on subsequent runs.

Usage:
  python3 discover_books.py                  # OverDrive only (default)
  python3 discover_books.py --library borrowbox
  python3 discover_books.py --library both
  python3 discover_books.py --full           # Force re-check all titles
"""

import re
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import sys
import csv
import os
import concurrent.futures
from collections import OrderedDict

GOODREADS_USER_ID = "19018093"
OVERDRIVE_LIBRARY = "glllibraries"
BORROWBOX_SITE = "southwark"
MIN_RATING = 3.85
MIN_RATING_COUNT = 10000
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

BB_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Sec-Ch-Ua': '"Google Chrome";v="131"',
}

def cache_path_for(library):
    return os.path.join(SCRIPT_DIR, f'seen_books_{library}.json')

FICTION_SHELVES = {
    'fiction', 'novels', 'mystery', 'thriller', 'fantasy',
    'science-fiction', 'romance', 'horror', 'historical-fiction',
    'young-adult', 'sci-fi', 'mystery-thriller', 'dystopian',
    'urban-fantasy', 'paranormal', 'crime-fiction', 'literary-fiction',
}
NONFICTION_SHELVES = {'nonfiction', 'non-fiction'}
FICTION_AUTHORS = {
    'agatha christie', 'charles dickens', 'george orwell',
    'p.g. wodehouse', 'virginia woolf', 'dylan thomas',
}

# ── Helpers ──

def fetch_url(url, retries=2, delay=1, headers=None):
    hdrs = headers or {'User-Agent': 'Mozilla/5.0'}
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode('utf-8', errors='replace')
        except Exception as e:
            if attempt < retries:
                time.sleep(delay)
            else:
                return None

def normalize_title(t):
    t = t.lower()
    t = re.split(r'[:\-\u2014\(\[]', t)[0].strip()
    t = re.sub(r'[^a-z0-9\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t

def print_status(msg):
    sys.stdout.write(f"\033[2m  → {msg}\033[0m\n")
    sys.stdout.flush()

# ── Cache: track previously seen books ──

def load_cache(library):
    """Load the seen books cache for a library. Returns dict keyed by normalized title."""
    path = cache_path_for(library)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def save_cache(cache, library):
    """Save the seen books cache for a library."""
    path = cache_path_for(library)
    with open(path, 'w') as f:
        json.dump(cache, f, indent=2)
    print_status(f"Cache saved: {len(cache)} books in {path}")

# ── Step 1: Fetch "read" shelf ──

def fetch_read_shelf():
    print_status("Fetching Goodreads 'read' shelf...")
    read_books = set()
    for page in range(1, 15):
        url = f"https://www.goodreads.com/review/list_rss/{GOODREADS_USER_ID}?shelf=read&page={page}"
        xml = fetch_url(url)
        if not xml:
            break
        items = re.findall(r'<item>(.*?)</item>', xml, re.DOTALL)
        if not items:
            break
        for item in items:
            title_m = re.search(r'<title><!\[CDATA\[(.*?)\]\]></title>', item)
            if title_m:
                read_books.add(normalize_title(title_m.group(1)))
        print_status(f"  Read shelf page {page}: {len(items)} books (total so far: {len(read_books)})")
    print_status(f"Total read books: {len(read_books)}")
    return read_books

# ── Step 2: Crawl OverDrive catalog ──

def fetch_od_page(page):
    url = f"https://thunder.api.overdrive.com/v2/libraries/{OVERDRIVE_LIBRARY}/media?format=audiobook-overdrive&subject=111&page={page}&perPage=100"
    data = fetch_url(url)
    if not data:
        return []
    try:
        d = json.loads(data)
    except:
        return []
    items = d.get('items', [])
    results = []
    for item in items:
        title = item.get('title', '')
        authors = [c['name'] for c in item.get('creators', []) if c.get('role') == 'Author']
        author = authors[0] if authors else 'Unknown'
        avail = item.get('isAvailable', False)
        holds = item.get('holdsCount', 0)
        wait = item.get('estimatedWaitDays', 0)
        pub_date = item.get('publishDate', '')
        if pub_date:
            pub_date = pub_date[:10]  # keep YYYY-MM-DD
        results.append({
            'title': title,
            'author': author,
            'available': avail,
            'holds': holds,
            'wait_days': wait,
            'pub_date': pub_date,
            'reserve_id': item.get('reserveId', ''),
            'source': 'OverDrive',
        })
    return results

def crawl_overdrive():
    print_status("Crawling OverDrive non-fiction audiobook catalog...")
    all_books = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(fetch_od_page, p): p for p in range(1, 25)}
        for f in concurrent.futures.as_completed(futures):
            books = f.result()
            if books:
                all_books.extend(books)
    print_status(f"OverDrive catalog: {len(all_books)} non-fiction audiobooks")
    return all_books

# ── Step 2b: Crawl BorrowBox catalog ──

def fetch_bb_page(page):
    offset = (page - 1) * 60
    url = f"https://{BORROWBOX_SITE}.borrowbox.com/search?q=&type=audiobook&fiction=false&currentPage={page}&pageSize=60&offset={offset}"
    html = fetch_url(url, headers=BB_HEADERS)
    if not html:
        return []
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except:
        return []
    products = data.get('props', {}).get('pageProps', {}).get('dehydratedState', {}).get('queries', [{}])[0].get('state', {}).get('data', {}).get('products', [])
    results = []
    for p in products:
        if p.get('format') != 'MP3':
            continue
        if p.get('fiction', True):
            continue
        title = p.get('title', '')
        authors = [a['name'] for a in p.get('authors', []) if a.get('performerType') == 'Author']
        author = authors[0] if authors else 'Unknown'
        status = p.get('availability', {}).get('status', 'UNKNOWN')
        avail = status == 'AVAILABLE'
        dur_secs = p.get('formatSpecifics', {}).get('totalDuration', 0)
        dur_hrs = dur_secs // 3600
        dur_mins = (dur_secs % 3600) // 60
        duration = f'{dur_hrs}h{dur_mins}m'
        results.append({
            'title': title,
            'author': author,
            'available': avail,
            'holds': 0,
            'wait_days': 0,
            'duration': duration,
            'isbn': p.get('isbn', ''),
            'source': 'BorrowBox',
        })
    return results

def crawl_borrowbox():
    print_status("Crawling BorrowBox non-fiction audiobook catalog...")
    all_books = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(fetch_bb_page, p): p for p in range(1, 45)}
        for f in concurrent.futures.as_completed(futures):
            books = f.result()
            if books:
                all_books.extend(books)
    print_status(f"BorrowBox catalog: {len(all_books)} non-fiction audiobooks")
    return all_books

# ── Step 3: Filter out read books & deduplicate ──

def filter_and_dedup(catalog, read_books):
    print_status("Filtering out already-read books and deduplicating...")
    seen = {}
    filtered = []
    excluded_read = 0
    for book in catalog:
        norm = normalize_title(book['title'])
        if norm in read_books:
            excluded_read += 1
            continue
        if norm in seen:
            continue
        seen[norm] = True
        book['norm_title'] = norm
        filtered.append(book)
    print_status(f"After filtering: {len(filtered)} remaining ({excluded_read} already read, {len(catalog) - len(filtered) - excluded_read} duplicates)")
    return filtered, excluded_read

# ── Step 4: Goodreads rating lookup ──

SKIP_TITLE_FRAGMENTS = (
    'summary of', 'summary:', 'summary &', 'summary and',
    'study guide', 'workbook for', 'workbook:', 'workbook based',
    'analysis of', 'analysis:', 'review and analysis',
    'collection set', 'box set', 'boxed set',
    'companion to', 'guide to ', 'sparknotes',
)

def _pick_best_match(html, query_title):
    """Parse all search results from a Goodreads search page and pick the best
    match for query_title. Returns dict with rating/count or None."""
    import math

    title_positions = [
        (m.end(), m.group(1))
        for m in re.finditer(
            r'<a class="bookTitle"[^>]*>\s*<span[^>]*>([^<]+)</span>',
            html,
        )
    ]
    candidates = []
    for rm in re.finditer(
        r'(\d+\.\d+)\s*avg rating\s*(?:—|&mdash;|&#x2014;)\s*([\d,]+)\s*rating',
        html,
    ):
        pos = rm.start()
        closest = None
        for tend, ttitle in title_positions:
            if tend <= pos:
                closest = ttitle
            else:
                break
        if closest:
            candidates.append({
                'title': closest.strip(),
                'rating': float(rm.group(1)),
                'count': int(rm.group(2).replace(',', '')),
            })

    if not candidates:
        return None

    norm_query = normalize_title(query_title)
    qwords = set(norm_query.split())
    best = None
    best_score = float('-inf')

    for c in candidates[:20]:
        title_lower = c['title'].lower()
        if any(s in title_lower for s in SKIP_TITLE_FRAGMENTS):
            continue
        norm_c = normalize_title(c['title'])
        if norm_c == norm_query:
            sim = 100
        elif norm_query and (norm_query in norm_c or norm_c in norm_query):
            sim = 60
        else:
            cwords = set(norm_c.split())
            if qwords and cwords:
                overlap = len(qwords & cwords) / max(len(qwords), 1)
                sim = int(overlap * 40)
            else:
                sim = 0
        score = sim + math.log10(max(c['count'], 1)) * 5
        if score > best_score:
            best_score = score
            best = c

    if best is None:
        best = max(candidates, key=lambda c: c['count'])
    return best

def lookup_goodreads(book, idx, total):
    title = book['title']
    author = book['author']
    query = urllib.parse.quote(f"{title} {author}")
    url = f"https://www.goodreads.com/search?q={query}&search_type=books"
    html = fetch_url(url, retries=3, delay=2)
    if not html:
        return None

    best = _pick_best_match(html, title)
    if not best:
        return None

    rating = best['rating']
    count = best['count']

    shelf_links = re.findall(r'/genres/([a-z0-9-]+)', html)
    shelves_lower = set(s.lower() for s in shelf_links)

    has_fiction = bool(shelves_lower & FICTION_SHELVES)
    has_nonfiction = bool(shelves_lower & NONFICTION_SHELVES)

    if has_fiction and not has_nonfiction:
        return None

    if author.lower().strip() in FICTION_AUTHORS:
        return None

    return {'rating': rating, 'count': count}

def lookup_all_ratings(books):
    print_status(f"Looking up Goodreads ratings for {len(books)} books (batches of 10)...")
    results = [None] * len(books)
    batch_size = 10
    no_match = 0
    low_rating = 0
    low_count = 0

    for batch_start in range(0, len(books), batch_size):
        batch_end = min(batch_start + batch_size, len(books))
        batch = list(range(batch_start, batch_end))

        with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as ex:
            futures = {}
            for i in batch:
                futures[ex.submit(lookup_goodreads, books[i], i, len(books))] = i
            for f in concurrent.futures.as_completed(futures):
                i = futures[f]
                try:
                    results[i] = f.result()
                except:
                    results[i] = None

        done = min(batch_end, len(books))
        print_status(f"  Progress: {done}/{len(books)} looked up")

        time.sleep(1)

    qualifying = []
    for i, book in enumerate(books):
        gr = results[i]
        if gr is None:
            print_status(f"    ✗ {book['title']} — no GR match")
            no_match += 1
            continue
        if gr['rating'] < MIN_RATING:
            print_status(f"    ✗ {book['title']} — rating {gr['rating']:.2f} (too low)")
            low_rating += 1
            continue
        if gr['count'] < MIN_RATING_COUNT:
            print_status(f"    ✗ {book['title']} — {gr['count']:,} ratings (too few)")
            low_count += 1
            continue
        book['gr_rating'] = gr['rating']
        book['gr_count'] = gr['count']
        qualifying.append(book)
        print_status(f"    \033[92m✓ {book['title']} — {gr['rating']:.2f} ({gr['count']:,} ratings)\033[0m")

    print_status(f"Qualifying: {len(qualifying)} (no match: {no_match}, low rating: {low_rating}, low count: {low_count})")
    return qualifying, no_match, low_rating, low_count

# ── Step 5: Report ──

def print_report(qualifying, stats, library_name):
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"

    if not qualifying:
        print(f"\n  {BOLD}No qualifying books found for {library_name}.{RESET}\n")
        return

    qualifying.sort(key=lambda b: b['gr_rating'], reverse=True)

    available = [b for b in qualifying if b['available']]
    waitlist = [b for b in qualifying if not b['available']]

    title_w = min(max((len(b['title'][:50]) for b in qualifying), default=20), 50)
    author_w = min(max((len(b['author'][:30]) for b in qualifying), default=15), 30)
    has_duration = any(b.get('duration') for b in qualifying)

    def print_row(title, author, rating, count, duration, status, color=""):
        t = title[:title_w].ljust(title_w)
        a = author[:author_w].ljust(author_w)
        r = str(rating).ljust(4) if isinstance(rating, float) else rating.ljust(4)
        c = str(count).ljust(8) if isinstance(count, int) else count.ljust(8)
        s = status.ljust(12)
        dur_col = f" │ {duration.ljust(7)}" if has_duration else ""
        print(f"  │ {t} │ {a} │ {CYAN}{r}{RESET} │ {c}{dur_col} │ {color}{s}{RESET} │")

    extra_w = 11 if has_duration else 0
    def print_sep():
        print(f"  {DIM}{'─' * (title_w + author_w + 48 + extra_w)}{RESET}")

    print()
    print(f"  {BOLD}{'═' * (title_w + author_w + 48 + extra_w)}{RESET}")
    print(f"  {BOLD} HIGH-RATED NON-FICTION AUDIOBOOKS — {library_name.upper()}{RESET}")
    print(f"  {BOLD}{'═' * (title_w + author_w + 48 + extra_w)}{RESET}")

    if available:
        print()
        print(f"  {GREEN}{BOLD}▶ AVAILABLE NOW ({len(available)} titles){RESET}")
        print_sep()
        dur_hdr = "Length" if has_duration else ""
        print_row("Title", "Author", "Rate", "Ratings", dur_hdr, "Status")
        print_sep()
        for b in available:
            print_row(b['title'], b['author'], b['gr_rating'], b['gr_count'], b.get('duration', ''), "Borrow now", GREEN)
        print_sep()

    if waitlist:
        print()
        print(f"  {YELLOW}{BOLD}▶ ON WAITLIST ({len(waitlist)} titles){RESET}")
        print_sep()
        dur_hdr = "Length" if has_duration else ""
        print_row("Title", "Author", "Rate", "Ratings", dur_hdr, "Status")
        print_sep()
        for b in waitlist:
            if b.get('source') == 'BorrowBox':
                wait_info = "On loan"
            elif b['wait_days']:
                wait_info = f"{b['holds']}h/{b['wait_days']}d"
            else:
                wait_info = f"{b['holds']} holds"
            print_row(b['title'], b['author'], b['gr_rating'], b['gr_count'], b.get('duration', ''), wait_info, YELLOW)
        print_sep()

    print()
    print(f"  {BOLD}── Summary ──{RESET}")
    print(f"  Total crawled:        {stats['total_crawled']}")
    print(f"  Excluded (read):      {stats['excluded_read']}")
    if stats.get('cached_qualifying') or stats.get('cached_rejected'):
        print(f"  From cache (qual):    {stats.get('cached_qualifying', 0)}")
        print(f"  From cache (reject):  {stats.get('cached_rejected', 0)}")
        print(f"  New titles checked:   {stats.get('new_checked', 0)}")
    print(f"  No GR match:          {stats['no_match']}")
    print(f"  Low rating (<{MIN_RATING}):    {stats['low_rating']}")
    print(f"  Low count (<{MIN_RATING_COUNT}):   {stats['low_count']}")
    print(f"  {GREEN}{BOLD}Available now:        {len(available)}{RESET}")
    print(f"  {YELLOW}{BOLD}On waitlist:          {len(waitlist)}{RESET}")
    print(f"  {BOLD}Total qualifying:     {len(qualifying)}{RESET}")
    print()

# ── Main ──

def run_library(library, read_books, full_mode):
    """Run the full pipeline for one library. Returns qualifying books list."""
    cache = load_cache(library) if not full_mode else {}

    if library == 'overdrive':
        library_name = 'OverDrive (Libby)'
        catalog = crawl_overdrive()
    else:
        library_name = 'BorrowBox'
        catalog = crawl_borrowbox()

    if cache:
        cached_q = [v for v in cache.values() if v.get('qualifying')]
        print_status(f"Cache loaded: {len(cache)} previously checked, {len(cached_q)} previously qualifying")
    else:
        print_status("No cache found — running full scan")

    filtered, excluded_read = filter_and_dedup(catalog, read_books)

    # Split into new vs cached books
    new_books = []
    cached_qualifying = []
    cached_rejected = 0
    for book in filtered:
        norm = book['norm_title']
        if norm in cache:
            entry = cache[norm]
            if entry.get('qualifying'):
                entry['available'] = book['available']
                entry['holds'] = book['holds']
                entry['wait_days'] = book['wait_days']
                if book.get('duration'):
                    entry['duration'] = book['duration']
                cached_qualifying.append(entry)
            else:
                cached_rejected += 1
        else:
            new_books.append(book)

    if new_books:
        print_status(f"New titles to check: {len(new_books)} (skipping {len(filtered) - len(new_books)} previously checked)")
    else:
        print_status(f"No new titles found (all {len(filtered)} were previously checked)")

    # Only do Goodreads lookups for new books
    new_qualifying = []
    no_match = low_rating = low_count = 0
    if new_books:
        new_qualifying, no_match, low_rating, low_count = lookup_all_ratings(new_books)

    # Update cache with new results
    for book in new_books:
        norm = book['norm_title']
        is_qualifying = book in new_qualifying
        cache[norm] = {
            'title': book['title'],
            'author': book['author'],
            'available': book['available'],
            'holds': book['holds'],
            'wait_days': book['wait_days'],
            'norm_title': norm,
            'qualifying': is_qualifying,
            'gr_rating': book.get('gr_rating'),
            'gr_count': book.get('gr_count'),
            'source': book.get('source', library),
        }
        if book.get('duration'):
            cache[norm]['duration'] = book['duration']
        if book.get('isbn'):
            cache[norm]['isbn'] = book['isbn']
        if book.get('reserve_id'):
            cache[norm]['reserve_id'] = book['reserve_id']

    # Remove cached entries for books no longer in the catalog
    catalog_norms = {normalize_title(b['title']) for b in catalog}
    removed = [k for k in cache if k not in catalog_norms]
    for k in removed:
        del cache[k]
    if removed:
        print_status(f"Removed {len(removed)} books no longer in catalog from cache")

    save_cache(cache, library)

    all_qualifying = cached_qualifying + new_qualifying

    stats = {
        'total_crawled': len(catalog),
        'excluded_read': excluded_read,
        'no_match': no_match,
        'low_rating': low_rating,
        'low_count': low_count,
        'cached_qualifying': len(cached_qualifying),
        'cached_rejected': cached_rejected,
        'new_checked': len(new_books),
    }

    print_report(all_qualifying, stats, library_name)
    export_csv(all_qualifying, f'discover_books_{library}.csv')
    return all_qualifying

def main():
    full_mode = '--full' in sys.argv

    # Parse --library flag
    library = 'overdrive'
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == '--library' and i + 1 < len(sys.argv):
            library = sys.argv[i + 1].lower()
            break
        elif arg.startswith('--library='):
            library = arg.split('=', 1)[1].lower()
            break

    if library not in ('overdrive', 'borrowbox', 'both'):
        print(f"Unknown library: {library}. Use: overdrive, borrowbox, or both")
        sys.exit(1)

    libraries = ['overdrive', 'borrowbox'] if library == 'both' else [library]
    mode_str = "full scan" if full_mode else "incremental"
    lib_str = " & ".join(l.title() for l in libraries)
    print(f"\n\033[1m  Discovering high-rated non-fiction audiobooks — {lib_str} ({mode_str})\033[0m\n")

    read_books = fetch_read_shelf()

    all_qualifying = []
    for lib in libraries:
        qualifying = run_library(lib, read_books, full_mode)
        all_qualifying.extend(qualifying)

    if len(libraries) > 1:
        export_csv(all_qualifying, 'discover_books_results.csv')

def export_csv(qualifying, filename):
    csv_path = os.path.join(SCRIPT_DIR, filename)
    qualifying.sort(key=lambda b: b.get('gr_rating', 0), reverse=True)
    with open(csv_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['Title', 'Author', 'Rating', 'Rating Count', 'Source', 'Available', 'Duration', 'Holds', 'Wait (days)'])
        for b in qualifying:
            writer.writerow([
                b['title'],
                b['author'],
                b.get('gr_rating', ''),
                b.get('gr_count', ''),
                b.get('source', ''),
                'Yes' if b['available'] else 'No',
                b.get('duration', ''),
                b.get('holds', 0),
                b.get('wait_days', 0),
            ])
    print(f"  Results exported to {csv_path}")

if __name__ == '__main__':
    main()
