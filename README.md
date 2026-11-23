# New Albums from Followed TIDAL Artists

`NewAlbumsThisYear.py` collects the recent and upcoming releases of every artist you follow on TIDAL and writes them to a single, self-contained page, `new_albums.html`. The page opens in your browser when the script finishes.

## Features

- Releases from the last 365 days and the next 365 days (configurable in the script).
- One entry per release: TIDAL lists the same release in several qualities, and the script keeps the best one.
- Release type (Album, EP, Single, Live), audio quality, cover, and the TIDAL review text.
- Genres from MusicBrainz, with an optional Last.fm fallback.
- Links to the TIDAL app, the TIDAL web player, and Spotify when the album exists there.
- Filters for date range, type, year, artist, genre, and free text search.
- Responsive layout for desktop and phone, with a dark/light theme toggle.
- Incremental updates: existing releases are read back from `new_albums.html`, so each run only looks up new ones.

## Requirements

- Python 3.9 or newer
- A TIDAL account that follows the artists you want to track

```
pip install -r requirements.txt
```

## Usage

```
python NewAlbumsThisYear.py
python NewAlbumsThisYear.py --fast
```

On the first run the script prints a TIDAL login URL. Open it, authorize, and the login is saved to `tidal_session.json`. Later runs reuse and refresh it automatically.

`--fast` skips the genre backfill of saved releases and avoids slow MusicBrainz lookups when a Last.fm key is set.

## Optional configuration

Set these environment variables before running.

| Variable | Purpose |
| --- | --- |
| `LASTFM_API_KEY` | Enables Last.fm as a genre source. Much faster than MusicBrainz alone. |
| `SPOTIFY_CLIENT_ID` | Spotify app client ID, needed for Spotify links. |
| `SPOTIFY_CLIENT_SECRET` | Spotify app client secret, needed for Spotify links. |

Create a free Spotify app at <https://developer.spotify.com/dashboard> to get the two Spotify values. Without them the page shows no Spotify links.

PowerShell example:

```
$env:LASTFM_API_KEY = "your-key"
$env:SPOTIFY_CLIENT_ID = "your-id"
$env:SPOTIFY_CLIENT_SECRET = "your-secret"
python NewAlbumsThisYear.py
```

Constants at the top of the script control the date range (`DAYS_BACK`, `DAYS_AHEAD`) and retry intervals (`BACKFILL_RETRY_DAYS`, `SPOTIFY_RETRY_DAYS`).

## Data sources

| Data | Source |
| --- | --- |
| Releases, covers, quality, reviews | TIDAL (`tidalapi`) |
| Genres | MusicBrainz, Last.fm (optional) |
| Spotify links | Spotify Web API (optional) |

MusicBrainz allows one request per second, so the first run with many new releases takes a while.

## Files

| File | Purpose |
| --- | --- |
| `NewAlbumsThisYear.py` | The generator script, including the HTML template. |
| `new_albums.html` | Generated page. It also stores the release data, so keep it between runs. |
| `tidal_session.json` | Your TIDAL login. Ignored by git. Never commit or share it. |
| `requirements.txt` | Python dependencies. |

## Security

`tidal_session.json` grants access to your TIDAL account. It is listed in `.gitignore`. Keep your Last.fm and Spotify credentials in environment variables, not in the script.
