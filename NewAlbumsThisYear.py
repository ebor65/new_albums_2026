import json
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


def get_genres(artist_name, album_name):
    """Look up the album's genres on MusicBrainz. TIDAL's API has no genre field.

    Returns a list of genre names, or [] if no confident match is found.
    """
    try:
        query = f'releasegroup:"{album_name}" AND artist:"{artist_name}"'
        hits = mb_get(MB_URL, {"query": query, "limit": 1}).get("release-groups", [])
        if not hits or hits[0].get("score", 0) < 90:
            return []
        group = mb_get(f"{MB_URL}/{hits[0]['id']}", {"inc": "genres"})
        genres = sorted(group.get("genres", []), key=lambda g: -g.get("count", 0))
        return [g["name"] for g in genres[:4]]
    except Exception:
        return []


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
            "description": get_description(album),
            "app_link": f"tidal://album/{album.id}",
            "web_link": f"https://tidal.com/browse/album/{album.id}",
            "cover": album.image(320) if album.cover else "",
        }
    )

albums.sort(key=lambda a: a["date"], reverse=True)

# "</" is escaped so album text can never close the <script> tag
data_json = json.dumps(albums, ensure_ascii=False).replace("</", "<\\/")

page = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>New Albums</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; background: #fafafa; color: #222; }
  .filters { position: sticky; top: 0; z-index: 1; display: flex; flex-wrap: wrap; gap: .75rem 1.25rem; align-items: center; padding: .75rem 1rem; margin-bottom: 1rem; background: #fff; border: 1px solid #ddd; border-radius: 8px; }
  .filters label { display: flex; gap: .35rem; align-items: center; }
  .filters input[type=search] { min-width: 12rem; }
  ul { list-style: none; padding: 0; }
  li { display: flex; gap: 1rem; padding: 1rem; margin-bottom: 1rem; background: #fff; border: 1px solid #ddd; border-radius: 8px; }
  img { width: 160px; height: 160px; object-fit: cover; border-radius: 4px; flex-shrink: 0; }
  h2 { margin: 0 0 .25rem; font-size: 1.2rem; }
  .meta { color: #666; margin: 0 0 .5rem; }
  .badge { display: inline-block; padding: 0 .5rem; border-radius: 1rem; background: #e8eefc; color: #234; font-size: .8rem; }
  a { color: #0066cc; }
  @media (prefers-color-scheme: dark) {
    body { background: #181818; color: #eee; }
    .filters, li { background: #242424; border-color: #444; }
    .meta { color: #aaa; }
    .badge { background: #2c3a5a; color: #dde; }
    a { color: #6db3ff; }
  }
</style>
</head>
<body>
<h1>New albums from followed artists</h1>
<div class="filters">
  <label>From <input type="date" id="from"></label>
  <label>To <input type="date" id="to"></label>
  <label><input type="checkbox" class="kind" value="Album" checked> Albums</label>
  <label><input type="checkbox" class="kind" value="EP" checked> EPs</label>
  <label><input type="checkbox" class="kind" value="Single" checked> Singles</label>
  <label><input type="checkbox" class="kind" value="Live" checked> Live</label>
  <label>Year <select id="year"><option value="">All</option></select></label>
  <label>Artist <select id="artist"><option value="">All</option></select></label>
  <label>Genre <select id="genre"><option value="">All</option></select></label>
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
  for (const a of shown) {
    const li = el("li");
    if (a.cover) li.append(el("img", "", {src: a.cover, alt: ""}));
    const box = el("div");
    box.append(el("h2", a.artist + " – " + a.album));
    const meta = el("p", a.date.slice(0, 4) + " · released " + a.date + " · ", {class: "meta"});
    meta.append(el("span", a.kind, {class: "badge"}));
    if (a.quality) meta.append(" · " + a.quality);
    box.append(meta);
    box.append(el("p", "Genre: " + (a.genres.length ? a.genres.join(", ") : "unknown")));
    if (a.description) box.append(el("p", a.description));
    box.append(el("a", "Open in TIDAL app", {href: a.app_link}));
    box.append(" · ");
    box.append(el("a", "Web player", {href: a.web_link, target: "_blank"}));
    li.append(box);
    list.append(li);
  }
}
document.querySelectorAll(".filters input, .filters select").forEach(i => i.addEventListener("input", render));
render();
</script>
</body>
</html>
""".replace("__DATA__", data_json)

out_file.write_text(page, encoding="utf-8")
print(f"\nAdded {new_count} new releases. Saved {len(albums)} releases to {out_file}")
webbrowser.open(out_file.as_uri())
