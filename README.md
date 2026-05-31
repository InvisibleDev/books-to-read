# books-to-read

Discovers high-rated non-fiction audiobooks available to borrow from **OverDrive (Libby)** and/or **BorrowBox**, cross-referenced against **Goodreads** ratings and your personal read shelf.

## What it does

1. Fetches your Goodreads "read" shelf to build an exclusion list.
2. Crawls the non-fiction audiobook catalog of the configured library (OverDrive or BorrowBox).
3. Filters out books you've already read and deduplicates.
4. Looks up Goodreads ratings for remaining titles (batched, parallel).
5. Prints a formatted terminal report split into "Available Now" and "On Waitlist".
6. Exports results to a CSV file sorted by rating.

A local JSON cache (`seen_books_overdrive.json` / `seen_books_borrowbox.json`) persists Goodreads lookup results between runs, so only newly appeared titles get checked on subsequent runs.

## Configuration

Edit the constants near the top of `discover_books.py`:

| Constant | Default | Description |
|---|---|---|
| `GOODREADS_USER_ID` | `"19018093"` | Your Goodreads numeric user ID (from profile URL) |
| `OVERDRIVE_LIBRARY` | `"glllibraries"` | OverDrive library slug (from `thunder.api.overdrive.com`) |
| `BORROWBOX_SITE` | `"southwark"` | BorrowBox subdomain (e.g. `southwark.borrowbox.com`) |
| `MIN_RATING` | `3.85` | Minimum Goodreads average rating |
| `MIN_RATING_COUNT` | `10000` | Minimum number of Goodreads ratings |

**Finding your OverDrive library slug:** open the OverDrive/Libby catalog in a browser, search for a book, and inspect the API URL — the slug appears in the path after `/libraries/`.

**Finding your BorrowBox subdomain:** visit your library's BorrowBox portal; the subdomain is the part before `.borrowbox.com`.

## Usage

```bash
# OverDrive (Libby) only — default
python3 discover_books.py

# BorrowBox only
python3 discover_books.py --library borrowbox

# Both libraries
python3 discover_books.py --library both

# Force re-check all titles (ignores cache)
python3 discover_books.py --full
python3 discover_books.py --library both --full
```

No external dependencies — uses only Python 3 standard library (`urllib`, `re`, `json`, `csv`, `concurrent.futures`).

## Output

**Terminal:** a table of qualifying books grouped by availability, then a summary block.

**CSV files:**
- `discover_books_overdrive.csv` — OverDrive results
- `discover_books_borrowbox.csv` — BorrowBox results
- `discover_books_results.csv` — combined results (only written when `--library both`)

Columns: `Title`, `Author`, `Rating`, `Rating Count`, `Source`, `Available`, `Duration`, `Holds`, `Wait (days)`.

## Caching

On each run the script:
- Loads the per-library cache of previously seen titles.
- Skips Goodreads lookups for titles already in the cache.
- Prunes cache entries for titles that have disappeared from the catalog.
- Saves the updated cache.

Use `--full` to bypass the cache and re-check every title (useful after changing rating thresholds or when the catalog has changed substantially).

## Fiction filtering

The script attempts to exclude fiction that slips through the "non-fiction" catalog filter by:
- Checking Goodreads genre shelves against a known fiction shelf list.
- Hard-coding a short list of fiction authors.

Titles tagged only with fiction shelves (and not `nonfiction`/`non-fiction`) are dropped. This is best-effort; edge cases may appear in results.
