import argparse
import json
import os
import re
import time
import webbrowser
from datetime import date, timedelta
from pathlib import Path

import requests
import tidalapi

# How far back to collect releases. The page lets you narrow the range further.
DAYS_BACK = 365
# Upcoming releases are kept up to this many days ahead. TIDAL uses far-future
# placeholder dates (e.g. 2500-12-31) for some releases, which are dropped.
DAYS_AHEAD = 365

parser = argparse.ArgumentParser(description="Build new_albums.html from followed TIDAL artists.")
parser.add_argument(
    "--fast",
    action="store_true",
    help="skip the genre backfill of existing releases and avoid slow MusicBrainz lookups "
    "when LASTFM_API_KEY is set (without a key, only the MusicBrainz album lookup is used)",
)
FAST = parser.parse_args().fast
# Releases without genres are retried at most once per this many days
BACKFILL_RETRY_DAYS = 30

session = tidalapi.Session()
# First run: prints a URL to authorize in the browser, then saves the tokens to
# tidal_session.json. Later runs reuse (and auto-refresh) the saved login.
# Keep this file private: it grants access to your TIDAL account.
session.login_session_file(Path(__file__).with_name("tidal_session.json"))


def fetch_all(fetch, page_size=50):
    """Call a paged tidalapi method until it returns no more items."""
    items, offset = [], 0
    while True:
        page = fetch(limit=page_size, offset=offset)
        items.extend(page)
        if len(page) < page_size:
            return items
        offset += page_size


start_date = date.today() - timedelta(days=DAYS_BACK)
end_date = date.today() + timedelta(days=DAYS_AHEAD)
followed_artists = fetch_all(session.user.favorites.artists)

print(f"Checking {len(followed_artists)} followed artists for releases since {start_date}...\n")


MB_URL = "https://musicbrainz.org/ws/2/release-group"
MB_HEADERS = {"User-Agent": "TidalNewAlbums/1.0"}
_last_mb_call = 0.0


def mb_get(url, params):
    """GET from MusicBrainz, limited to 1 request per second (their rule)."""
    global _last_mb_call
    wait = 1.1 - (time.time() - _last_mb_call)
    if wait > 0:
        time.sleep(wait)
    _last_mb_call = time.time()
    resp = requests.get(url, params={**params, "fmt": "json"}, headers=MB_HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


MB_ARTIST_URL = "https://musicbrainz.org/ws/2/artist"
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
# Optional: set the LASTFM_API_KEY environment variable to enable the Last.fm fallback.
LASTFM_KEY = os.environ.get("LASTFM_API_KEY", "")
# Last.fm tag counts are normalised to 0-100; lower counts are mostly noise.
LASTFM_MIN_COUNT = 20
LASTFM_JUNK = {
    "seen live", "favorites", "favourites", "favorite", "favourite", "albums i own",
    "owned", "love", "loved", "awesome", "good", "best", "beautiful", "male vocalists",
    "female vocalists", "female vocalist", "male vocalist", "singer-songwriter",
    "under 2000 listeners", "spotify", "tidal", "new", "all", "check out", "usa", "uk",
    "american", "british", "german", "french", "swedish", "canadian", "australian",
}


def _clean_genres(names, limit=4):
    """Title-case-insensitive de-duplication, keeping order."""
    seen, out = set(), []
    for n in names:
        key = n.casefold().strip()
        if key and key not in seen:
            seen.add(key)
            out.append(n.strip())
    return out[:limit]


def _mb_genres(url):
    """Genre names of one MusicBrainz entity, most-voted first."""
    data = mb_get(url, {"inc": "genres"})
    genres = sorted(data.get("genres", []), key=lambda g: -g.get("count", 0))
    return [g["name"] for g in genres]


def mb_album_genres(artist_name, album_name):
    query = f'releasegroup:"{album_name}" AND artist:"{artist_name}"'
    hits = mb_get(MB_URL, {"query": query, "limit": 1}).get("release-groups", [])
    if not hits or hits[0].get("score", 0) < 90:
        return []
    return _mb_genres(f"{MB_URL}/{hits[0]['id']}")


def mb_artist_genres(artist_name):
    hits = mb_get(MB_ARTIST_URL, {"query": f'artist:"{artist_name}"', "limit": 1}).get("artists", [])
    if not hits or hits[0].get("score", 0) < 90:
        return []
    return _mb_genres(f"{MB_ARTIST_URL}/{hits[0]['id']}")


def lastfm_tags(method, **params):
    """Return filtered genre-like tags from a Last.fm *.getTopTags call."""
    resp = requests.get(
        LASTFM_URL,
        params={"method": method, "api_key": LASTFM_KEY, "format": "json", "autocorrect": 1, **params},
        headers=MB_HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    tags = resp.json().get("toptags", {}).get("tag", [])
    skip = {str(v).casefold() for v in params.values()}
    return [
        t["name"]
        for t in tags
        if int(t.get("count", 0)) >= LASTFM_MIN_COUNT
        and t["name"].casefold() not in LASTFM_JUNK
        and t["name"].casefold() not in skip
        and not re.fullmatch(r"\d{2,4}s?", t["name"])
    ]


def get_genres(artist_name, album_name):
    """Look up genres. TIDAL's API has no genre field.

    Order: MusicBrainz album, Last.fm album, MusicBrainz artist, Last.fm artist.
    In --fast mode MusicBrainz (1 request per second) is skipped when a Last.fm key is set.
    Returns a list of genre names, or [] if nothing is found.
    """
    use_mb = not (FAST and LASTFM_KEY)
    steps = []
    if use_mb:
        steps.append(lambda: mb_album_genres(artist_name, album_name))
    if LASTFM_KEY:
        steps.append(lambda: lastfm_tags("album.getTopTags", artist=artist_name, album=album_name))
    if use_mb and not FAST:
        steps.append(lambda: mb_artist_genres(artist_name))
    if LASTFM_KEY:
        steps.append(lambda: lastfm_tags("artist.getTopTags", artist=artist_name))
    for step in steps:
        try:
            genres = _clean_genres(step())
        except Exception:
            continue
        if genres:
            return genres
    return []


SPOTIFY_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
# Releases without a Spotify match are searched again after this many days
# (new and upcoming releases often reach Spotify later).
SPOTIFY_RETRY_DAYS = 7
_spotify = {"token": "", "expires": 0.0, "disabled": not (SPOTIFY_ID and SPOTIFY_SECRET)}


def _spotify_token():
    if time.time() < _spotify["expires"]:
        return _spotify["token"]
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(SPOTIFY_ID, SPOTIFY_SECRET),
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    _spotify["token"] = data["access_token"]
    _spotify["expires"] = time.time() + data.get("expires_in", 3600) - 60
    return _spotify["token"]


def _norm(text):
    return re.sub(r"[^0-9a-z]+", "", text.casefold())


def get_spotify(artist_name, album_name):
    """Return the open.spotify.com URL of the matching album, or '' if not found.

    Needs the SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET environment variables
    (free app at developer.spotify.com). Without them no lookup is done.
    Returns None if the lookup could not run (so the release is not marked as checked).
    """
    if _spotify["disabled"]:
        return None
    try:
        resp = requests.get(
            "https://api.spotify.com/v1/search",
            params={"q": f'album:"{album_name}" artist:"{artist_name}"', "type": "album", "limit": 10},
            headers={"Authorization": f"Bearer {_spotify_token()}"},
            timeout=20,
        )
        if resp.status_code == 429:
            print("Spotify rate limit reached, skipping further Spotify lookups this run.")
            _spotify["disabled"] = True
            return None
        resp.raise_for_status()
    except Exception as e:
        print(f"Spotify lookup failed, skipping further lookups this run: {e}")
        _spotify["disabled"] = True
        return None
    want_album, want_artist = _norm(album_name), _norm(artist_name)
    for item in resp.json().get("albums", {}).get("items", []):
        got = _norm(item["name"])
        artists = [_norm(a["name"]) for a in item.get("artists", [])]
        album_ok = got == want_album or got.startswith(want_album) or want_album.startswith(got)
        artist_ok = any(a == want_artist or want_artist in a or a in want_artist for a in artists if a)
        if album_ok and artist_ok:
            return item["external_urls"]["spotify"]
    return ""


def get_description(album):
    """Return the TIDAL album review as plain text, or '' if none exists."""
    try:
        text = album.review()
    except Exception:
        return ""
    # Reviews contain tags like [wimpLink artistId="123"]Name[/wimpLink]; keep only the name
    text = re.sub(r"\[/?wimpLink[^\]]*\]", "", text)
    return text.strip()


def quality_info(album):
    """Return (rank, label) for an album. Higher rank = better version."""
    tags = {str(t).upper() for t in (getattr(album, "media_metadata_tags", None) or [])}
    base = str(getattr(album, "audio_quality", "") or "").upper()
    labels = []
    rank = 0
    if "HIRES_LOSSLESS" in tags or base in ("HI_RES", "HI_RES_LOSSLESS"):
        labels.append("Hi-Res Lossless")
        rank = 3
    elif "LOSSLESS" in tags or base == "LOSSLESS":
        labels.append("Lossless")
        rank = 2
    elif base:
        labels.append(base.replace("_", " ").title())
        rank = 1
    if "DOLBY_ATMOS" in tags:
        labels.append("Dolby Atmos")
        rank += 0.5
    if "SONY_360RA" in tags:
        labels.append("360 Reality Audio")
    return rank, " + ".join(labels)


def release_kind(album):
    """Return 'Live', 'Album', 'EP' or 'Single'."""
    title = f"{album.name or ''} {getattr(album, 'version', '') or ''}"
    if re.search(r"\blive\b", title, re.IGNORECASE):
        return "Live"
    kind = str(getattr(album, "type", "") or "").upper()
    return {"EP": "EP", "SINGLE": "Single"}.get(kind, "Album")


# Step 1: collect releases. TIDAL often lists one release several times (different audio
# qualities, explicit/clean). Keep only the best version of each.
best = {}
for artist in followed_artists:
    releases = []
    # get_albums returns full albums, get_ep_singles returns EPs and singles
    for fetch in (artist.get_albums, artist.get_ep_singles):
        try:
            releases.extend(fetch_all(fetch))
        except Exception as e:
            print(f"Skipping {fetch.__name__} for {artist.name}: {e}")
    for album in releases:
        if not (album.release_date and start_date <= album.release_date.date() <= end_date):
            continue
        main_artist = getattr(getattr(album, "artist", None), "name", None) or artist.name
        key = (
            main_artist.casefold().strip(),
            (album.name or "").casefold().strip(),
            (getattr(album, "version", "") or "").casefold().strip(),
            release_kind(album),
        )
        rank, label = quality_info(album)
        # Prefer higher quality; on a tie prefer the explicit version
        score = (rank, bool(getattr(album, "explicit", False)))
        if key not in best or score > best[key]["score"]:
            best[key] = {
                "score": score,
                "album": album,
                "artist_name": main_artist,
                "quality": label,
            }

print(f"Found {len(best)} unique releases. Looking up genres and descriptions...\n")

out_file = Path(__file__).with_name("new_albums.html")


def load_existing(path):
    """Read the release list embedded in a previously generated page ([] if none)."""
    try:
        text = path.read_text(encoding="utf-8")
        match = re.search(r"const ALBUMS = (\[.*?\]);\s*const \$ =", text, re.S)
        return json.loads(match.group(1)) if match else []
    except Exception as e:
        print(f"Could not read existing {path.name}, starting fresh: {e}")
        return []


def entry_key(artist, name, kind):
    return (artist.casefold().strip(), name.casefold().strip(), kind)


albums = load_existing(out_file)
# Remove saved entries with implausible far-future dates (placeholders such as 2500-12-31)
valid = [a for a in albums if a["date"] <= f"{end_date:%Y-%m-%d}"]
if len(valid) < len(albums):
    print(f"Removed {len(albums) - len(valid)} entries dated after {end_date}")
albums = valid
known_keys = {entry_key(a["artist"], a["album"], a["kind"]) for a in albums}
known_links = {a.get("app_link") for a in albums}
print(f"Loaded {len(albums)} existing releases from {out_file.name}\n")

# Step 2: fetch genre and description only for releases not already in the file
new_count = 0
for item in best.values():
    album = item["album"]
    name = album.name + (f" ({album.version})" if getattr(album, "version", None) else "")
    kind = release_kind(album)
    if (
        entry_key(item["artist_name"], name, kind) in known_keys
        or f"tidal://album/{album.id}" in known_links
    ):
        continue
    new_count += 1
    print(f"NEW: {item['artist_name']} - {name} [{kind}] ({album.release_date:%Y-%m-%d})")
    albums.append(
        {
            "artist": item["artist_name"],
            "album": name,
            "kind": kind,
            "date": f"{album.release_date:%Y-%m-%d}",
            "quality": item["quality"],
            "genres": get_genres(item["artist_name"], album.name),
            "genres_checked": f"{date.today():%Y-%m-%d}",
            "description": get_description(album),
            "spotify_link": get_spotify(item["artist_name"], album.name) or "",
            "spotify_checked": f"{date.today():%Y-%m-%d}" if not _spotify["disabled"] else "",
            "app_link": f"tidal://album/{album.id}",
            "web_link": f"https://tidal.com/browse/album/{album.id}",
            "cover": album.image(320) if album.cover else "",
        }
    )

# Step 3: retry genres for saved releases that have none. Skipped in --fast mode, and
# each release is retried at most once per BACKFILL_RETRY_DAYS.
if not FAST:
    retry_before = f"{date.today() - timedelta(days=BACKFILL_RETRY_DAYS):%Y-%m-%d}"
    todo = [a for a in albums if not a.get("genres") and a.get("genres_checked", "") < retry_before]
    if todo:
        print(f"\nRetrying genres for {len(todo)} releases without genres...")
    for a in todo:
        # Strip the " (version)" suffix that was appended to the saved album name
        base_name = re.sub(r"\s*\([^()]*\)$", "", a["album"]) if "(" in a["album"] else a["album"]
        a["genres"] = get_genres(a["artist"], base_name)
        a["genres_checked"] = f"{date.today():%Y-%m-%d}"
        print(f"  {a['artist']} - {a['album']}: {', '.join(a['genres']) or 'still unknown'}")

# Step 4: look for Spotify links of saved releases that have none yet
if not _spotify["disabled"]:
    spotify_retry_before = f"{date.today() - timedelta(days=SPOTIFY_RETRY_DAYS):%Y-%m-%d}"
    todo = [a for a in albums if not a.get("spotify_link") and a.get("spotify_checked", "") < spotify_retry_before]
    if todo:
        print(f"\nLooking up Spotify links for {len(todo)} releases...")
    found = 0
    for a in todo:
        base_name = re.sub(r"\s*\([^()]*\)$", "", a["album"]) if "(" in a["album"] else a["album"]
        link = get_spotify(a["artist"], base_name)
        if link is None:
            break
        a["spotify_link"] = link
        a["spotify_checked"] = f"{date.today():%Y-%m-%d}"
        found += bool(link)
    if todo:
        print(f"  Found {found} Spotify links.")
elif not (SPOTIFY_ID and SPOTIFY_SECRET):
    print("\nSet SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to add Spotify links.")

albums.sort(key=lambda a: a["date"], reverse=True)

# "</" is escaped so album text can never close the <script> tag
data_json = json.dumps(albums, ensure_ascii=False).replace("</", "<\\/")

page = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f1117" media="(prefers-color-scheme: dark)">
<meta name="theme-color" content="#f5f6fa" media="(prefers-color-scheme: light)">
<title>New Albums</title>
<script>
  // Apply saved theme before first paint to avoid a flash
  try { const t = localStorage.getItem("theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) {}
</script>
<style>
  :root {
    --bg: #f5f6fa; --surface: #fff; --surface-2: #eef0f6; --border: #dde1ea;
    --text: #1b1e27; --muted: #6a7184; --accent: #4f46e5; --accent-soft: #e6e4fd;
    --shadow: 0 1px 2px rgba(20,24,40,.06), 0 4px 16px rgba(20,24,40,.06);
    color-scheme: light;
  }
  :root[data-theme="dark"] {
    --bg: #0f1117; --surface: #181b24; --surface-2: #21252f; --border: #2b303c;
    --text: #e8eaf1; --muted: #9097aa; --accent: #8b87ff; --accent-soft: #2a2858;
    --shadow: 0 1px 2px rgba(0,0,0,.4), 0 4px 16px rgba(0,0,0,.35);
    color-scheme: dark;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #0f1117; --surface: #181b24; --surface-2: #21252f; --border: #2b303c;
      --text: #e8eaf1; --muted: #9097aa; --accent: #8b87ff; --accent-soft: #2a2858;
      --shadow: 0 1px 2px rgba(0,0,0,.4), 0 4px 16px rgba(0,0,0,.35);
      color-scheme: dark;
    }
  }
  * { box-sizing: border-box; }
  body { font-family: Inter, system-ui, -apple-system, "Segoe UI", sans-serif; max-width: 1240px; margin: 0 auto; padding: max(1.5rem, env(safe-area-inset-top)) max(1rem, env(safe-area-inset-right)) max(3rem, env(safe-area-inset-bottom)) max(1rem, env(safe-area-inset-left)); background: var(--bg); color: var(--text); line-height: 1.5; transition: background .2s, color .2s; }
  header { display: flex; justify-content: space-between; align-items: center; gap: 1rem; margin-bottom: 1rem; }
  h1 { margin: 0; font-size: 1.6rem; letter-spacing: -.02em; }
  #theme { width: 2.5rem; height: 2.5rem; border-radius: 50%; border: 1px solid var(--border); background: var(--surface); color: var(--text); font-size: 1.15rem; cursor: pointer; box-shadow: var(--shadow); }
  #theme:hover { border-color: var(--accent); }
  .filters { position: sticky; top: .5rem; z-index: 2; display: flex; flex-wrap: wrap; gap: .6rem 1rem; align-items: center; padding: .85rem 1rem; margin-bottom: 1.25rem; background: color-mix(in srgb, var(--surface) 88%, transparent); backdrop-filter: blur(10px); border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow); }
  .filters label { display: flex; gap: .4rem; align-items: center; font-size: .9rem; color: var(--muted); }
  .filters input[type=checkbox] { accent-color: var(--accent); width: 1rem; height: 1rem; }
  input[type=date], input[type=search], select { font: inherit; font-size: .9rem; padding: .35rem .6rem; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); color: var(--text); }
  input:focus, select:focus, button:focus-visible, a:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .filters input[type=search] { flex: 1; min-width: 12rem; }
  #count { margin-left: auto; font-size: .9rem; color: var(--accent); }
  ul { list-style: none; padding: 0; margin: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 520px), 1fr)); gap: 1rem; }
  #filter-toggle, .filters .row-toggle { display: none; }
  .filter-fields { display: contents; }
  .empty { grid-column: 1 / -1; text-align: center; color: var(--muted); padding: 2rem 0; }
  li { display: flex; gap: 1.1rem; padding: 1rem; background: var(--surface); border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow); transition: transform .15s, border-color .15s; }
  li:hover { transform: translateY(-2px); border-color: var(--accent); }
  img { width: 160px; height: 160px; object-fit: cover; border-radius: 10px; flex-shrink: 0; background: var(--surface-2); }
  h2 { margin: 0 0 .25rem; font-size: 1.15rem; line-height: 1.3; }
  .meta { color: var(--muted); font-size: .85rem; margin: 0 0 .6rem; display: flex; flex-wrap: wrap; gap: .4rem .6rem; align-items: center; }
  .badge { padding: .1rem .6rem; border-radius: 1rem; background: var(--accent-soft); color: var(--accent); font-size: .75rem; font-weight: 600; }
  .genres { display: flex; flex-wrap: wrap; gap: .35rem; margin: 0 0 .6rem; }
  .genre { padding: .1rem .55rem; border-radius: 6px; background: var(--surface-2); font-size: .78rem; color: var(--muted); }
  .desc { margin: 0 0 .7rem; font-size: .92rem; }
  .links { display: flex; flex-wrap: wrap; gap: .6rem; margin-top: auto; }
  .links a { padding: .3rem .8rem; border-radius: 8px; border: 1px solid var(--border); color: var(--accent); text-decoration: none; font-size: .85rem; font-weight: 500; }
  .links a:hover { background: var(--accent-soft); }
  .links a.spotify { color: #1db954; }
  .links a.spotify:hover { background: color-mix(in srgb, #1db954 15%, transparent); }
  .body { display: flex; flex-direction: column; min-width: 0; }
  .head { min-width: 0; }
  /* Touch devices: larger tap targets, 16px inputs (stops iOS zoom on focus) */
  @media (pointer: coarse) {
    input[type=date], input[type=search], select { font-size: 16px; min-height: 2.6rem; }
    .links a, #theme { min-height: 2.75rem; display: inline-flex; align-items: center; }
    #theme { width: 2.75rem; justify-content: center; }
    .filters input[type=checkbox] { width: 1.25rem; height: 1.25rem; }
    li:hover { transform: none; }
  }
  /* Tablet and phone */
  @media (max-width: 700px) {
    body { padding-top: max(.75rem, env(safe-area-inset-top)); }
    h1 { font-size: 1.25rem; }
    .filters { top: max(.25rem, env(safe-area-inset-top)); padding: .6rem .75rem; gap: .5rem; border-radius: 12px; }
    .filters .row-toggle { display: inline-flex; align-items: center; gap: .4rem; }
    #filter-toggle { display: inline-flex; padding: .35rem .8rem; min-height: 2.6rem; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); color: var(--text); font: inherit; cursor: pointer; }
    #filter-toggle[aria-expanded="true"] { border-color: var(--accent); color: var(--accent); }
    .filters input[type=search] { order: -1; flex: 1 1 100%; }
    #count { order: -1; margin-left: auto; }
    .filter-fields { display: none; flex-wrap: wrap; gap: .6rem 1rem; width: 100%; padding-top: .5rem; border-top: 1px solid var(--border); }
    .filters.open .filter-fields { display: flex; }
    .filter-fields label { flex: 1 1 45%; flex-direction: column; align-items: stretch; gap: .2rem; }
    .filter-fields label:has(.kind) { flex: 0 0 auto; flex-direction: row; align-items: center; }
    ul { grid-template-columns: 1fr; gap: .75rem; }
    /* Compact card: small cover beside title, details span the full width */
    li { display: grid; grid-template-columns: 96px 1fr; gap: .5rem .85rem; padding: .8rem; }
    li .body { display: contents; }
    img { width: 96px; height: 96px; border-radius: 8px; }
    h2 { font-size: 1rem; }
    .genres, .desc, .links { grid-column: 1 / -1; margin: 0; }
    .links a { flex: 1; justify-content: center; text-align: center; }
  }
</style>
</head>
<body>
<header>
  <h1>New albums from followed artists</h1>
  <button id="theme" type="button" title="Toggle dark / light theme" aria-label="Toggle theme"></button>
</header>
<div class="filters">
  <button type="button" id="filter-toggle" aria-expanded="false" aria-controls="filter-fields">Filters</button>
  <div class="filter-fields" id="filter-fields">
  <label>From <input type="date" id="from"></label>
  <label>To <input type="date" id="to"></label>
  <label><input type="checkbox" class="kind" value="Album" checked> Albums</label>
  <label><input type="checkbox" class="kind" value="EP" checked> EPs</label>
  <label><input type="checkbox" class="kind" value="Single" checked> Singles</label>
  <label><input type="checkbox" class="kind" value="Live" checked> Live</label>
  <label>Year <select id="year"><option value="">All</option></select></label>
  <label>Artist <select id="artist"><option value="">All</option></select></label>
  <label>Genre <select id="genre"><option value="">All</option></select></label>
  </div>
  <input type="search" id="q" placeholder="Search artist, album, genre">
  <strong id="count"></strong>
</div>
<ul id="list"></ul>
<script>
const ALBUMS = __DATA__;
const $ = id => document.getElementById(id);
const dates = ALBUMS.map(a => a.date).sort();
$("from").value = dates[0] || "";
// Start with released albums only; move "To" forward to see upcoming releases
$("to").value = new Date().toLocaleDateString("sv-SE");

function fillSelect(id, values) {
  const sel = $(id);
  for (const v of [...new Set(values)].sort((a, b) => a.localeCompare(b, undefined, {numeric: true})))
    sel.append(el("option", v, {value: v}));
}
fillSelect("year", ALBUMS.map(a => a.date.slice(0, 4)));
fillSelect("artist", ALBUMS.map(a => a.artist));
fillSelect("genre", ALBUMS.flatMap(a => a.genres));

function el(tag, text, attrs) {
  const e = document.createElement(tag);
  if (text) e.textContent = text;
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  return e;
}

function render() {
  const from = $("from").value, to = $("to").value;
  const kinds = new Set([...document.querySelectorAll(".kind:checked")].map(c => c.value));
  const q = $("q").value.trim().toLowerCase();
  const year = $("year").value, artist = $("artist").value, genre = $("genre").value;
  const shown = ALBUMS.filter(a =>
    (!from || a.date >= from) && (!to || a.date <= to) && kinds.has(a.kind) &&
    (!year || a.date.startsWith(year)) && (!artist || a.artist === artist) &&
    (!genre || a.genres.includes(genre)) &&
    (!q || (a.artist + " " + a.album + " " + a.genres.join(" ")).toLowerCase().includes(q)));
  $("count").textContent = shown.length + " of " + ALBUMS.length;
  const list = $("list");
  list.replaceChildren();
  if (!shown.length) list.append(el("li", "No releases match these filters.", {class: "empty"}));
  for (const a of shown) {
    const li = el("li");
    if (a.cover) li.append(el("img", "", {src: a.cover, alt: "", loading: "lazy", decoding: "async"}));
    const box = el("div", "", {class: "body"});
    const head = el("div", "", {class: "head"});
    head.append(el("h2", a.artist + " – " + a.album));
    const meta = el("p", "", {class: "meta"});
    meta.append(el("span", a.kind, {class: "badge"}));
    meta.append(el("span", "Released " + a.date));
    if (a.quality) meta.append(el("span", a.quality));
    head.append(meta);
    box.append(head);
    const genres = el("div", "", {class: "genres"});
    if (a.genres.length) for (const g of a.genres) genres.append(el("span", g, {class: "genre"}));
    else genres.append(el("span", "Genre unknown", {class: "genre"}));
    box.append(genres);
    if (a.description) box.append(el("p", a.description, {class: "desc"}));
    const links = el("div", "", {class: "links"});
    links.append(el("a", "Open in TIDAL app", {href: a.app_link}));
    links.append(el("a", "Web player", {href: a.web_link, target: "_blank", rel: "noopener"}));
    if (a.spotify_link) links.append(el("a", "Open in Spotify", {href: a.spotify_link, target: "_blank", rel: "noopener", class: "spotify"}));
    box.append(links);
    li.append(box);
    list.append(li);
  }
}
// Filter panel toggle (phone only; always open on wide screens)
$("filter-toggle").addEventListener("click", () => {
  const open = document.querySelector(".filters").classList.toggle("open");
  $("filter-toggle").setAttribute("aria-expanded", open);
});
// Theme toggle: explicit choice is saved; without one the OS setting applies
const root = document.documentElement;
const isDark = () => root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
const syncIcon = () => { $("theme").textContent = isDark() ? "☀" : "☾"; };
$("theme").addEventListener("click", () => {
  const next = isDark() ? "light" : "dark";
  root.dataset.theme = next;
  try { localStorage.setItem("theme", next); } catch (e) {}
  syncIcon();
});
syncIcon();
document.querySelectorAll(".filters input, .filters select").forEach(i => i.addEventListener("input", render));
render();
</script>
</body>
</html>
""".replace("__DATA__", data_json)

out_file.write_text(page, encoding="utf-8")
print(f"\nAdded {new_count} new releases. Saved {len(albums)} releases to {out_file}")
webbrowser.open(out_file.as_uri())
