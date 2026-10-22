"""Import a public Google Photos shared album into Immich.

Paste a shared-album link (photos.app.goo.gl/... or photos.google.com/share/...) into the
web form; every photo/video is downloaded in original quality, uploaded to Immich and
added to an Immich album with the same name. Re-running an album is safe: Immich
de-duplicates by checksum and already-present assets are just (re)added to the album.

Google exposes no official API for this, so the album is read through the same internal
endpoints the photos.google.com web page uses. If Google changes them, this breaks.

Standard library only. Config via env: IMMICH_URL, IMMICH_API_KEY, IMMICH_PUBLIC_URL, PORT, WORKERS, STATE_PATH.
"""

import html
import json
import mimetypes
import os
import re
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IMMICH_URL = os.environ.get("IMMICH_URL", "http://immich_server:2283").rstrip("/")
IMMICH_API_KEY = os.environ["IMMICH_API_KEY"]
PORT = int(os.environ.get("PORT", "8080"))
WORKERS = int(os.environ.get("WORKERS", "4"))

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
BATCH_URL = "https://photos.google.com/_/PhotosUi/data/batchexecute"
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".webm", ".mts", ".m2ts"}

jobs = {}  # job id -> dict, newest shown first
jobs_lock = threading.Lock()

# Per Google album: {"immich_album": id, "items": {google_item_id: immich_asset_id}} so re-runs
# only fetch photos added since last time and keep filling the same Immich album.
STATE_PATH = os.environ.get("STATE_PATH", "/data/state.json")
state_lock = threading.Lock()


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def save_album_state(album_id, immich_album, imported, link, title, credited=()):
    with state_lock:
        state = load_state()
        entry = state.setdefault(album_id, {"items": {}})
        if immich_album:
            entry["immich_album"] = immich_album
        entry["items"].update(imported)
        # Google item ids whose "Taken by" is settled (added, or a description already existed).
        entry["credited"] = sorted(set(entry.get("credited", [])) | set(credited))
        entry.update(link=link, title=title, synced=time.strftime("%b %d %H:%M"))
        write_state(state)


def forget_album(album_id):
    with state_lock:
        state = load_state()
        if state.pop(album_id, None) is not None:
            write_state(state)


# ---------------------------------------------------------------- Google Photos


def http(url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def rpc(calls):
    """Run batchexecute RPCs; returns {call_index: parsed_payload}."""
    freq = [[[rpcid, json.dumps(args), None, str(i)] for i, (rpcid, args) in enumerate(calls)]]
    body = urllib.parse.urlencode({"f.req": json.dumps(freq)}).encode()
    with http(BATCH_URL, body, {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}) as r:
        text = r.read().decode()
    text = text.split("\n", 1)[1] if text.startswith(")]}'") else text
    rows = []
    try:
        rows = json.loads(text)
    except ValueError:
        # Chunked format: alternating length lines and JSON arrays.
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("["):
                try:
                    rows += json.loads(line)
                except ValueError:
                    pass
    out = {}
    for row in rows:
        if isinstance(row, list) and len(row) > 2 and row[0] == "wrb.fr" and row[2]:
            idx = int(row[-1]) if isinstance(row[-1], str) and row[-1].isdigit() else len(out)
            out[idx] = json.loads(row[2])
    return out


def resolve_album(link):
    """Follow a share link to photos.google.com/share/<id>?key=<key>."""
    with http(link.strip()) as r:
        final = r.geturl()
        page = r.read().decode(errors="replace")
    candidates = [final]
    m = re.search(r'<link\s+rel="canonical"\s+href="([^"]+)"', page)
    if m:
        candidates.append(html.unescape(m.group(1)))
    for c in candidates:
        u = urllib.parse.urlparse(c)
        m = re.search(r"/share/([A-Za-z0-9_-]+)", u.path)
        key = urllib.parse.parse_qs(u.query).get("key", [None])[0]
        if m and key:
            return m.group(1), key
    raise ValueError("That doesn't look like a public Google Photos shared-album link "
                     "(link sharing may be turned off for the album).")


def walk(node):
    """Yield every nested list and string in a decoded RPC payload."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, list):
            yield n
            stack.extend(n)
        elif isinstance(n, dict):
            stack.extend(n.values())
        elif isinstance(n, str):
            yield n


def find_people(payload):
    """Map Google user id -> display name.

    Album members appear as lists shaped like [obfuscated_id, gaia_id, ..., [full_name, _, _, first_name]]
    (index 11); photos reference their contributor by the obfuscated "AF1Qip…" id, so both ids are
    mapped. Matched by shape rather than position so it survives layout shuffles.
    """
    people = {}
    for n in walk(payload):
        if (isinstance(n, list) and len(n) > 11 and isinstance(n[1], str) and isinstance(n[11], list)
                and n[11] and isinstance(n[11][0], str) and n[11][0].strip()):
            for uid in (n[0], n[1]):
                if isinstance(uid, str) and uid:
                    people.setdefault(uid, n[11][0].strip())
    return people


def contributor(entry, people):
    for s in walk(entry):
        if isinstance(s, str) and s in people:
            return people[s]
    return None


def list_album(album_id, key):
    """Return (title, items) where items are dicts with id, url, ts (ms), by (contributor name or None)."""
    title, items, token, pages, people, raw = None, [], None, 0, {}, []
    while True:
        pages += 1
        res = rpc([("snAcKc", [album_id, token, None, key])]).get(0)
        if not res:
            if pages == 1:
                raise ValueError("Google returned no album data (album private or format changed).")
            break
        if pages == 1:
            # Kept for debugging format changes: docker exec <container> cat /tmp/last-album.json
            with open("/tmp/last-album.json", "w") as f:
                json.dump(res, f)
        if title is None and len(res) > 3 and res[3]:
            title = res[3][1]
        people.update(find_people(res))
        raw.extend(res[1] or [])
        token = res[2] if len(res) > 2 else None
        if not token or pages > 200:
            break
    for it in raw:
        try:
            items.append({"id": it[0], "url": it[1][0], "ts": it[2], "by": contributor(it, people)})
        except (IndexError, TypeError):
            pass
    return title or "Google Photos import", items


def fill_metadata(items, key):
    """Add filename / created timestamp for each item (100 per RPC)."""
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        try:
            res = rpc([("fDcn4b", [it["id"], None, key]) for it in chunk])
        except Exception:
            continue
        for idx, payload in res.items():
            try:
                info = payload[0]
                if idx < len(chunk) and info[0] == chunk[idx]["id"]:
                    chunk[idx]["filename"] = info[2]
                    if isinstance(info[3], (int, float)):
                        chunk[idx]["ts"] = info[3]
            except (IndexError, TypeError):
                pass


# ---------------------------------------------------------------------- Immich


def immich(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{IMMICH_URL}/api{path}", data=data, method=method,
        headers={"x-api-key": IMMICH_API_KEY, "Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def upload(path, filename, ts_ms):
    """Multipart upload streamed from disk; returns (asset_id, status)."""
    when = datetime.fromtimestamp((ts_ms or time.time() * 1000) / 1000, timezone.utc).isoformat()
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    fields = {"fileCreatedAt": when, "fileModifiedAt": when, "filename": filename}
    head = "".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in fields.items()
    )
    safe_name = filename.replace('"', "")
    head += (f'--{boundary}\r\nContent-Disposition: form-data; name="assetData"; filename="{safe_name}"\r\n'
             f"Content-Type: {ctype}\r\n\r\n")
    tail = f"\r\n--{boundary}--\r\n".encode()
    size = len(head.encode()) + os.path.getsize(path) + len(tail)

    def body():
        yield head.encode()
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                yield chunk
        yield tail

    req = urllib.request.Request(
        f"{IMMICH_URL}/api/assets", data=body(), method="POST",
        headers={"x-api-key": IMMICH_API_KEY, "Accept": "application/json",
                 "Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(size)},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        res = json.loads(r.read())
    return res["id"], res.get("status", "created")


def find_or_create_album(name):
    for a in immich("GET", "/albums") or []:
        if a.get("albumName") == name:
            return a["id"], False
    return immich("POST", "/albums", {"albumName": name})["id"], True


# ------------------------------------------------------------------------ jobs


def job_update(job, **kw):
    with jobs_lock:
        job.update(kw)


def import_item(job, it):
    name = it.get("filename") or f"{it['id']}.jpg"
    is_video = os.path.splitext(name)[1].lower() in VIDEO_EXT
    fd, tmp = tempfile.mkstemp(prefix="gpi-", dir=job["tmpdir"])
    try:
        with os.fdopen(fd, "wb") as out, http(it["url"] + ("=dv" if is_video else "=d"), timeout=300) as r:
            cd = r.headers.get("Content-Disposition", "")
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
            if m and not it.get("filename"):
                name = urllib.parse.unquote(m.group(1))
            while chunk := r.read(1 << 20):
                out.write(chunk)
        asset_id, status = upload(tmp, name, it.get("ts"))
        with jobs_lock:
            job["done"] += 1
            job["dupes" if status == "duplicate" else "created"] += 1
            job["asset_ids"].append(asset_id)
            job["imported"][it["id"]] = asset_id
            if it.get("by"):
                job["credits"].append((asset_id, it["by"], status == "duplicate", it["id"]))
    except Exception as e:
        with jobs_lock:
            job["done"] += 1
            job["failed"].append(f"{name}: {e}")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def apply_credits(job):
    """Set 'Taken by <name>' as the description, never overwriting an existing one.

    Returns the Google item ids that are settled. Assets that may already have a description
    (duplicates, or ones imported on an earlier run) are checked first.
    """
    by_person, settled = {}, set()
    for asset_id, name, check_first, gid in job["credits"]:
        if check_first:
            try:
                if (immich("GET", f"/assets/{asset_id}").get("exifInfo") or {}).get("description"):
                    settled.add(gid)
                    continue
            except Exception:
                continue  # e.g. asset deleted from Immich; try again next run
        by_person.setdefault(name, []).append((asset_id, gid))
    for name, pairs in by_person.items():
        for i in range(0, len(pairs), 500):
            chunk = pairs[i:i + 500]
            immich("PUT", "/assets", {"ids": [a for a, _ in chunk], "description": f"Taken by {name}"})
            settled.update(g for _, g in chunk)
            job["tagged"] += len(chunk)
    return settled


def run_job(job):
    try:
        job_update(job, state="Reading Google album…")
        album_id, key = resolve_album(job["link"])
        title, items = list_album(album_id, key)
        known = load_state().get(album_id, {})
        new_items = [it for it in items if it["id"] not in known.get("items", {})]
        # Previously imported photos still missing their "Taken by" get it on this run.
        credited = set(known.get("credited", []))
        for it in items:
            asset_id = known.get("items", {}).get(it["id"])
            if asset_id and it.get("by") and it["id"] not in credited:
                job["credits"].append((asset_id, it["by"], True, it["id"]))
        job_update(job, title=job["album_name"] or known.get("title") or title, skipped=len(items) - len(new_items))
        items = new_items
        job_update(job, total=len(items), state="Reading file details…")
        fill_metadata(items, key)
        counts = {}
        for it in items:
            counts[it.get("by") or "Unknown"] = counts.get(it.get("by") or "Unknown", 0) + 1
        job_update(job, contributors=sorted(counts.items(), key=lambda kv: -kv[1]))
        if job["preview"]:
            job["credits"].clear()
            sample = [it.get("filename") or it["id"] for it in items[:15]]
            job_update(job, state="Preview only — nothing imported", sample=sample, finished=True)
            return
        job_update(job, state="Downloading & uploading…")
        with tempfile.TemporaryDirectory(prefix="gpi-") as td:
            job["tmpdir"] = td
            with ThreadPoolExecutor(WORKERS) as pool:
                list(pool.map(lambda it: import_item(job, it), items))
        immich_album = None
        if job["asset_ids"]:
            job_update(job, state="Adding to Immich album…")
            immich_album = known.get("immich_album") if not job["album_name"] else None
            if immich_album:
                try:
                    immich("GET", f"/albums/{immich_album}")
                except urllib.error.HTTPError:
                    immich_album = None  # album was deleted in Immich; make a new one
            if not immich_album:
                immich_album, _ = find_or_create_album(job["title"])
            ids = list(dict.fromkeys(job["asset_ids"]))
            for i in range(0, len(ids), 500):
                immich("PUT", f"/albums/{immich_album}/assets", {"ids": ids[i:i + 500]})
            job_update(job, album_url=f"/albums/{immich_album}")
        settled = set()
        if job["credits"]:
            job_update(job, state="Adding “Taken by” descriptions…")
            try:
                settled = apply_credits(job)
            except Exception as e:
                job["failed"].append(f"descriptions: {e}")
        # Only successful items are recorded, so failures are retried on the next run.
        save_album_state(album_id, immich_album, job["imported"], job["link"], job["title"], settled)
        state = "Done" if not job["failed"] else f"Done with {len(job['failed'])} failures"
        job_update(job, state=state, finished=True)
    except Exception as e:
        traceback.print_exc()
        job_update(job, state=f"Failed: {e}", finished=True, error=True)


def start_job(link, album_name, preview):
    job = {
        "id": uuid.uuid4().hex[:8], "link": link, "album_name": album_name.strip(), "preview": preview,
        "title": album_name.strip() or "…", "state": "Queued", "total": 0, "done": 0, "created": 0,
        "dupes": 0, "failed": [], "asset_ids": [], "credits": [], "contributors": [], "imported": {}, "skipped": 0, "tagged": 0, "finished": False, "started": time.strftime("%b %d %H:%M"), "t": time.time(),
    }
    with jobs_lock:
        jobs[job["id"]] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job


sync_lock = threading.Lock()


def sync_saved(album_ids=None):
    """Re-check saved albums one at a time (in the background) for newly added photos."""
    if not sync_lock.acquire(blocking=False):
        return  # a sync is already running

    def worker():
        try:
            for album_id, entry in load_state().items():
                if (album_ids is None or album_id in album_ids) and entry.get("link"):
                    job = start_job(entry["link"], "", False)
                    while not job["finished"]:
                        time.sleep(1)
        finally:
            sync_lock.release()
    threading.Thread(target=worker, daemon=True).start()


# ------------------------------------------------------------------------- web
# Styled after the Immich web app: its colors, rounded surfaces, Material Design Icons and
# album grid. Thumbnails and the logo are proxied from Immich so the API key stays server-side.

IMMICH_PUBLIC = (os.environ.get("IMMICH_PUBLIC_URL") or IMMICH_URL).rstrip("/")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Material Design Icons (the set Immich uses)
ICONS = {
    "sync": "M12,18A6,6 0 0,1 6,12C6,11 6.25,10.03 6.7,9.2L5.24,7.74C4.46,8.97 4,10.43 4,12A8,8 0 0,0 12,20V23L16,19L12,15M12,4V1L8,5L12,9V6A6,6 0 0,1 18,12C18,13 17.75,13.97 17.3,14.8L18.76,16.26C19.54,15.03 20,13.57 20,12A8,8 0 0,0 12,4Z",
    "close": "M19,6.41L17.59,5L12,10.59L6.41,5L5,6.41L10.59,12L5,17.59L6.41,19L12,13.41L17.59,19L19,17.59L13.41,12L19,6.41Z",
    "open": "M14,3V5H17.59L7.76,14.83L9.17,16.24L19,6.41V10H21V3M19,19H5V5H12V3H5C3.89,3 3,3.9 3,5V19A2,2 0 0,0 5,21H19A2,2 0 0,0 21,19V12H19V19Z",
    "eye": "M12,9A3,3 0 0,1 15,12A3,3 0 0,1 12,15A3,3 0 0,1 9,12A3,3 0 0,1 12,9M12,4.5C17,4.5 21.27,7.61 23,12C21.27,16.39 17,19.5 12,19.5C7,19.5 2.73,16.39 1,12C2.73,7.61 7,4.5 12,4.5M3.18,12C4.83,15.36 8.24,17.5 12,17.5C15.76,17.5 19.17,15.36 20.82,12C19.17,8.64 15.76,6.5 12,6.5C8.24,6.5 4.83,8.64 3.18,12Z",
    "download": "M2 12H4V17H20V12H22V17C22 18.11 21.11 19 20 19H4C2.9 19 2 18.11 2 17V12M12 15L17.55 9.54L16.13 8.13L13 11.25V2H11V11.25L7.88 8.13L6.46 9.55L12 15Z",
    "image": "M8.5,13.5L11,16.5L14.5,12L19,18H5M21,19V5C21,3.89 20.1,3 19,3H5A2,2 0 0,0 3,5V19A2,2 0 0,0 5,21H19A2,2 0 0,0 21,19Z",
}


def icon(name, size=20):
    return (f'<svg class="mdi" width="{size}" height="{size}" viewBox="0 0 24 24" aria-hidden="true">'
            f'<path fill="currentColor" d="{ICONS[name]}"/></svg>')


def pill(text, kind=""):
    return f'<span class="pill {kind}">{html.escape(text)}</span>'


def thumb(asset_id, cls):
    if not asset_id:
        return f'<div class="{cls} empty">{icon("image", 32)}</div>'
    return f'<img class="{cls}" src="/thumb/{asset_id}" alt="" loading="lazy">'


_covers = {"t": 0, "map": {}}


def album_covers():
    """Immich album id -> cover asset id, refreshed at most every 30 s."""
    if time.time() - _covers["t"] > 30:
        _covers["t"] = time.time()
        try:
            _covers["map"] = {a["id"]: a.get("albumThumbnailAssetId") for a in immich("GET", "/albums") or []}
        except Exception:
            pass  # keep the last known covers
    return _covers["map"]


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Google Photos Importer</title>
<link rel="icon" href="/logo">
<style>
:root{--bg:#fff;--fg:#000;--muted:#6b7280;--surface:#f5f5f5;--border:#e5e5e5;--input:#fff;--input-border:#d4d4d4;
--primary:rgb(66 80 175);--on-primary:#fff;--primary-soft:rgb(66 80 175/.12);--success:#15803d;--success-soft:#dcfce7;
--danger:#dc2626;--danger-soft:#fddede;--warning:#b45309;--warning-soft:#fef3c7}
@media (prefers-color-scheme:dark){:root{--bg:rgb(10 10 10);--fg:rgb(229 231 235);--muted:#a1a1a1;--surface:rgb(33 33 33);
--border:#2e2e2e;--input:#171717;--input-border:#3d3d3d;--primary:rgb(172 203 250);--on-primary:rgb(33 33 33);
--primary-soft:rgb(172 203 250/.14);--success:#4ade80;--success-soft:rgb(74 222 128/.14);--danger:#fa6967;
--danger-soft:rgb(250 105 103/.14);--warning:#fbbf24;--warning-soft:rgb(251 191 36/.14)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 "GoogleSans","Google Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:var(--primary);text-decoration:none}
.mdi{flex:none;vertical-align:middle}
header{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:12px;height:64px;padding:0 16px;
background:var(--bg);border-bottom:1px solid var(--border)}
header img{width:32px;height:32px}
header h1{flex:1;margin:0;font-size:20px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
main{max-width:960px;margin:0 auto;padding:24px 16px 48px}
h2{margin:0;font-size:14px;font-weight:600;letter-spacing:.02em;text-transform:uppercase;color:var(--primary)}
section{margin-bottom:32px}
.head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}
.card{background:var(--surface);border-radius:24px;padding:20px}
label{display:block;font-size:14px;font-weight:500;margin:0 0 6px}
.hint{font-weight:400;color:var(--muted)}
input[type=url],input[type=text]{width:100%;margin-bottom:16px;padding:12px 14px;font:inherit;font-size:16px;color:var(--fg);
background:var(--input);border:1px solid var(--input-border);border-radius:12px;outline:none}
input:focus{border-color:var(--primary);box-shadow:0 0 0 1px var(--primary)}
.row{display:flex;gap:10px;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:10px 18px;font:inherit;font-weight:500;
border-radius:12px;border:1px solid var(--primary);background:var(--primary);color:var(--on-primary);cursor:pointer}
.btn.ghost{background:transparent;color:var(--primary)}
.btn.sm{padding:6px 14px;font-size:14px}
.btn:disabled{opacity:.6;cursor:default}
.row .btn{flex:1 1 140px}
.icon-btn{display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;padding:0;border:0;
border-radius:50%;background:transparent;color:var(--muted);cursor:pointer}
.icon-btn:hover{background:var(--primary-soft);color:var(--primary)}
form{margin:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:20px 16px}
@media (max-width:420px){.grid{grid-template-columns:1fr 1fr}}
.cover{display:block;width:100%;aspect-ratio:1;object-fit:cover;border-radius:12px;background:var(--surface)}
.cover.empty,.jthumb.empty{display:flex;align-items:center;justify-content:center;color:var(--muted)}
.album{position:relative;min-width:0}
a.cover-link{display:block;border-radius:12px;transition:transform .15s}
a.cover-link:hover{transform:scale(1.02)}
.album .actions{position:absolute;top:8px;right:8px;display:flex;gap:6px}
.album .icon-btn{width:32px;height:32px;background:rgb(0 0 0/.45);color:#fff;backdrop-filter:blur(4px)}
.album .icon-btn:hover{background:rgb(0 0 0/.7);color:#fff}
.album .meta{margin-top:8px}
.title{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub{font-size:13px;color:var(--muted)}
.album .sub{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.busy .sync-ico{animation:spin 1s linear infinite}
@media (max-width:480px){header .btn-label{display:none}header .btn{padding:6px 10px}}
@keyframes spin{to{transform:rotate(-360deg)}}
.job{display:flex;gap:16px;align-items:flex-start;margin-bottom:12px}
.jthumb{width:72px;height:72px;flex:none;object-fit:cover;border-radius:12px;background:var(--border)}
.job .body{flex:1;min-width:0}
.job .top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:500;background:var(--border);color:var(--fg)}
.pill.primary{background:var(--primary-soft);color:var(--primary)}
.pill.success{background:var(--success-soft);color:var(--success)}
.pill.danger{background:var(--danger-soft);color:var(--danger)}
.pill.warning{background:var(--warning-soft);color:var(--warning)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.bar{height:6px;background:var(--border);border-radius:999px;overflow:hidden;margin:10px 0 6px}
.bar i{display:block;height:100%;background:var(--primary);border-radius:999px;transition:width .4s}
.job a{display:inline-flex;align-items:center;gap:4px;margin-top:8px;font-weight:500}
details{margin-top:8px}summary{cursor:pointer;color:var(--danger);font-size:14px}
code{display:block;margin-top:6px;font:12px/1.5 ui-monospace,monospace;word-break:break-all;color:var(--muted)}
.empty-state{padding:32px 20px;text-align:center;color:var(--muted)}
</style></head><body>
<header><img src="/logo" alt="" onerror="this.remove()"><h1>Google Photos Importer</h1>
<a class="btn ghost sm" href="%%IMMICH%%"><span class="btn-label">Open Immich</span> %%OPEN%%</a></header>
<main>
<section><div class="head"><h2>Import a shared album</h2></div>
<form class="card" method="post" action="/import">
<label for="u">Shared album link</label>
<input id="u" name="url" type="url" required placeholder="https://photos.app.goo.gl/…" value="%%PREFILL%%">
<label for="n">Immich album name <span class="hint">· optional, defaults to the Google album's name</span></label>
<input id="n" name="name" type="text" placeholder="e.g. Colorado 2026">
<div class="row"><button class="btn" name="mode" value="import">%%DOWNLOAD%% Import</button>
<button class="btn ghost" name="mode" value="preview">%%EYE%% Preview</button></div>
</form></section>
<div id="live" data-active="%%ACTIVE%%">%%LIVE%%</div>
</main>
<script>
// Refresh progress in place (a full page reload would wipe a link being typed)
const live=document.getElementById('live');
async function poll(){
  try{const r=await fetch('/live',{cache:'no-store'});live.innerHTML=await r.text();
    if(r.headers.get('X-Active')==='1')setTimeout(poll,3000)}
  catch(e){setTimeout(poll,5000)}}
if(live.dataset.active==='1')setTimeout(poll,3000);
</script>
</body></html>"""


def saved_covers():
    """Google album link -> cover asset id for albums imported before."""
    covers = album_covers()
    return {e["link"]: covers.get(e.get("immich_album")) or next(iter(e.get("items", {}).values()), None)
            for e in load_state().values() if e.get("link")}


def render_jobs():
    link_cover = saved_covers()
    with jobs_lock:
        snapshot = sorted(jobs.values(), key=lambda j: j["t"], reverse=True)
        out = []
        for j in snapshot:
            pct = int(100 * j["done"] / j["total"]) if j["total"] else 0
            if j.get("error"):
                status = pill("Failed", "danger")
            elif not j["finished"]:
                status = pill("Running", "primary")
            elif j["preview"]:
                status = pill("Preview")
            elif j["failed"]:
                status = pill(f"{len(j['failed'])} failed", "warning")
            else:
                status = pill("Done", "success")
            parts = [f'<div class="card job">{thumb(j["asset_ids"][0] if j["asset_ids"] else link_cover.get(j["link"]), "jthumb")}<div class="body">'
                     f'<div class="top"><span class="title">{html.escape(j["title"])}</span>{status}</div>'
                     f'<div class="sub">{j["started"]} · {html.escape(j["state"])}</div>']
            if j["total"] and not j["preview"]:
                parts.append(f'<div class="bar"><i style="width:{pct}%"></i></div>'
                             f'<div class="sub">{j["done"]} of {j["total"]} · {j["created"]} new · '
                             f'{j["dupes"]} already in Immich · {len(j["failed"])} failed</div>')
            if j["skipped"]:
                parts.append(f'<div class="sub">{j["skipped"]} imported on an earlier run, skipped</div>')
            if j["finished"] and not j["total"] and j["skipped"] and not j.get("error"):
                parts.append('<div class="sub">No new photos since the last import.</div>')
            if j["tagged"]:
                parts.append(f'<div class="sub">Added “Taken by” to {j["tagged"]} photos</div>')
            if j["preview"] and j.get("sample"):
                parts.append(f'<div class="sub">{j["total"]} items found. First few:</div><code>'
                             + html.escape(", ".join(j["sample"])) + "</code>")
            if j["contributors"]:
                parts.append('<div class="chips">' + "".join(
                    pill(f"{n} · {c}") for n, c in j["contributors"]) + "</div>")
            if j.get("album_url"):
                parts.append(f'<a href="{IMMICH_PUBLIC}{j["album_url"]}">Open album in Immich {icon("open", 16)}</a>')
            if j["failed"]:
                parts.append("<details><summary>Show failures</summary><code>"
                             + "<br>".join(html.escape(f) for f in j["failed"][:50]) + "</code></details>")
            out.append("".join(parts) + "</div></div>")
        active = any(not j["finished"] for j in snapshot)
        running = {j["link"] for j in snapshot if not j["finished"]}
    if not out:
        return "", active, running
    return '<section><div class="head"><h2>Activity</h2></div>' + "".join(out) + "</section>", active, running


def render_saved(running):
    saved = [(k, v) for k, v in load_state().items() if v.get("link")]
    busy = sync_lock.locked()
    sync_all = (f'<form method="post" action="/sync"><button class="btn ghost sm"{" disabled" if busy else ""}>'
                f'{icon("sync", 18)} {"Checking…" if busy else "Sync all"}</button></form>')
    if not saved:
        return ('<section><div class="head"><h2>Saved albums</h2></div><div class="card empty-state">'
                "Albums you import show up here, so you can check them for new photos later.</div></section>")
    saved.sort(key=lambda kv: (kv[1].get("title") or "").lower())
    covers = saved_covers()
    cards = []
    for album_id, e in saved:
        aid = html.escape(album_id)
        title = html.escape(e.get("title") or "Untitled")
        immich_album = e.get("immich_album")
        cover = thumb(covers.get(e["link"]), "cover")
        if immich_album:
            cover = f'<a class="cover-link" href="{IMMICH_PUBLIC}/albums/{html.escape(immich_album)}">{cover}</a>'
        n = len(e.get("items", {}))
        cards.append(
            f'<div class="album{" busy" if e.get("link") in running else ""}">{cover}<div class="meta">'
            f'<div class="title" title="{title}">{title}</div>'
            f'<div class="sub">{n} item{"s" if n != 1 else ""}</div>'
            f'<div class="sub">Checked {html.escape(e.get("synced", "?"))}</div></div>'
            f'<div class="actions"><form method="post" action="/sync"><input type="hidden" name="album" value="{aid}">'
            f'<button class="icon-btn" title="Check for new photos" aria-label="Sync {title}">'
            f'<span class="sync-ico" style="display:inline-flex">{icon("sync", 18)}</span></button></form>'
            f'<form method="post" action="/forget" onsubmit="return confirm(\'Stop tracking this album? Its photos stay in Immich.\')">'
            f'<input type="hidden" name="album" value="{aid}">'
            f'<button class="icon-btn" title="Forget (keeps photos in Immich)" aria-label="Forget {title}">{icon("close", 18)}</button>'
            "</form></div></div>"
        )
    return (f'<section><div class="head"><h2>Saved albums</h2>{sync_all}</div>'
            f'<div class="grid">{"".join(cards)}</div></section>')


def render_live():
    jobs_html, active, running = render_jobs()
    return jobs_html + render_saved(running), active or sync_lock.locked()


_logo = {}


def fetch_logo():
    """Immich's own app icon, fetched once from the Immich server."""
    if "data" not in _logo:
        try:
            with urllib.request.urlopen(f"{IMMICH_URL}/apple-icon-180.png", timeout=10) as r:
                _logo["data"] = r.read()
        except Exception:
            return None
    return _logo["data"]


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/health":
            return self.send(200, "ok", "text/plain")
        if u.path == "/live":
            body, active = render_live()
            return self.send(200, body, headers={"X-Active": "1" if active else "0", "Cache-Control": "no-store"})
        if u.path.startswith("/thumb/"):
            return self.send_thumb(u.path[len("/thumb/"):])
        if u.path == "/logo":
            data = fetch_logo()
            if not data:
                return self.send(404, "not found", "text/plain")
            return self.send(200, data, "image/png", {"Cache-Control": "max-age=86400"})
        if u.path != "/":
            return self.send(404, "not found", "text/plain")
        q = urllib.parse.parse_qs(u.query)
        # Accept ?url= or ?text= (Android share sheets put the link in free text).
        raw = (q.get("url") or q.get("text") or [""])[0]
        m = re.search(r"https://\S+", raw)
        live, active = render_live()
        page = (PAGE.replace("%%IMMICH%%", html.escape(IMMICH_PUBLIC))
                .replace("%%OPEN%%", icon("open", 16)).replace("%%DOWNLOAD%%", icon("download"))
                .replace("%%EYE%%", icon("eye")).replace("%%ACTIVE%%", "1" if active else "0")
                .replace("%%PREFILL%%", html.escape(m.group(0) if m else "")).replace("%%LIVE%%", live))
        self.send(200, page)

    def send_thumb(self, asset_id):
        if not UUID_RE.fullmatch(asset_id):
            return self.send(404, "not found", "text/plain")
        req = urllib.request.Request(f"{IMMICH_URL}/api/assets/{asset_id}/thumbnail?size=thumbnail",
                                     headers={"x-api-key": IMMICH_API_KEY})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data, ctype = r.read(), r.headers.get("Content-Type", "image/jpeg")
        except Exception:
            return self.send(404, "not found", "text/plain")
        self.send(200, data, ctype, {"Cache-Control": "private, max-age=86400"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        album = (form.get("album") or [""])[0]
        if self.path == "/sync":
            sync_saved({album} if album else None)
            return self.send(303, "", headers={"Location": "/"})
        if self.path == "/forget":
            forget_album(album)
            return self.send(303, "", headers={"Location": "/"})
        if self.path != "/import":
            return self.send(404, "not found", "text/plain")
        link = (form.get("url") or [""])[0].strip()
        m = re.search(r"https://\S+", link)
        if m:
            start_job(m.group(0), (form.get("name") or [""])[0], (form.get("mode") or [""])[0] == "preview")
        self.send(303, "", headers={"Location": "/"})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    print(f"Listening on :{PORT}, Immich at {IMMICH_URL}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
