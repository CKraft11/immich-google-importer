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
    def worker():
        with sync_lock:
            for album_id, entry in load_state().items():
                if (album_ids is None or album_id in album_ids) and entry.get("link"):
                    job = start_job(entry["link"], "", False)
                    while not job["finished"]:
                        time.sleep(1)
    threading.Thread(target=worker, daemon=True).start()


# ------------------------------------------------------------------------- web

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Google Photos → Immich</title>{refresh}
<style>
:root{{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--fg:#1b1d21;--mute:#6b7280;--acc:#4250af;--bd:#e3e5ea}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111318;--card:#1b1e25;--fg:#e8eaee;--mute:#9aa1ad;--acc:#8c9cff;--bd:#2b2f38}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,sans-serif;padding:16px}}
main{{max-width:640px;margin:auto}} h1{{font-size:1.3rem;margin:.2em 0 .8em}}
.card{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;margin-bottom:14px}}
label{{display:block;font-size:.85rem;color:var(--mute);margin:.6em 0 .25em}}
input[type=url],input[type=text]{{width:100%;box-sizing:border-box;padding:12px;font-size:16px;border:1px solid var(--bd);border-radius:8px;background:var(--bg);color:var(--fg)}}
.row{{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}}
button{{flex:1;padding:12px;font-size:16px;border-radius:8px;border:1px solid var(--acc);background:var(--acc);color:#fff;font-weight:600}}
button.alt{{background:transparent;color:var(--acc)}}
.bar{{height:8px;background:var(--bd);border-radius:4px;overflow:hidden;margin:8px 0}} .bar i{{display:block;height:100%;background:var(--acc)}}
.mute{{color:var(--mute);font-size:.85rem}} .err{{color:#d33}} a{{color:var(--acc)}}
details{{margin-top:6px}} code{{font-size:.8rem;word-break:break-all}}
.album{{display:flex;align-items:center;gap:8px;padding:10px 0;border-top:1px solid var(--bd)}}
.album div{{flex:1;min-width:0}} .album form{{margin:0}}
.album button{{flex:none;padding:6px 10px;font-size:.85rem;font-weight:500}}
</style></head><body><main>
<h1>Google Photos → Immich</h1>
<form class="card" method="post" action="/import">
<label for="u">Shared album link</label>
<input id="u" name="url" type="url" required placeholder="https://photos.app.goo.gl/…" value="{prefill}">
<label for="n">Immich album name (optional — defaults to the Google album's name)</label>
<input id="n" name="name" type="text" placeholder="e.g. Colorado 2026">
<div class="row"><button name="mode" value="import">Import</button>
<button class="alt" name="mode" value="preview">Preview only</button></div>
</form>
{saved}
{jobs}
</main></body></html>"""


def render_jobs():
    with jobs_lock:
        snapshot = sorted(jobs.values(), key=lambda j: j["t"], reverse=True)
        out = []
        for j in snapshot:
            pct = int(100 * j["done"] / j["total"]) if j["total"] else 0
            parts = [f'<div class="card"><b>{html.escape(j["title"])}</b>'
                     f'<div class="mute">{j["started"]} · {html.escape(j["state"])}</div>']
            if j["total"] and not j["preview"]:
                parts.append(f'<div class="bar"><i style="width:{pct}%"></i></div>'
                             f'<div class="mute">{j["done"]}/{j["total"]} · {j["created"]} new · '
                             f'{j["dupes"]} already in Immich · {len(j["failed"])} failed</div>')
            if j["skipped"]:
                parts.append(f'<div class="mute">{j["skipped"]} imported on an earlier run — skipped</div>')
            if j["finished"] and not j["total"] and j["skipped"] and not j.get("error"):
                parts.append('<div class="mute">No new photos since the last import.</div>')
            if j["tagged"]:
                parts.append(f'<div class="mute">Added “Taken by” to {j["tagged"]} photos</div>')
            if j["preview"] and j.get("sample") is not None:
                parts.append(f'<div class="mute">{j["total"]} items found. First few:</div><code>'
                             + html.escape(", ".join(j["sample"])) + "</code>")
            if j["contributors"]:
                parts.append('<div class="mute">Taken by: ' + html.escape(
                    ", ".join(f"{n} ({c})" for n, c in j["contributors"])) + "</div>")
            if j.get("album_url"):
                parts.append(f'<div style="margin-top:8px"><a href="{IMMICH_PUBLIC}{j["album_url"]}">'
                             "Open album in Immich →</a></div>")
            if j["failed"]:
                parts.append('<details><summary class="err">Failures</summary><code>'
                             + "<br>".join(html.escape(f) for f in j["failed"][:50]) + "</code></details>")
            out.append("".join(parts) + "</div>")
        active = any(not j["finished"] for j in snapshot)
    return "".join(out), active


def render_saved():
    saved = [(k, v) for k, v in load_state().items() if v.get("link")]
    if not saved:
        return ""
    saved.sort(key=lambda kv: (kv[1].get("title") or "").lower())
    rows = []
    for album_id, e in saved:
        aid = html.escape(album_id)
        rows.append(
            f'<div class="album"><div><b>{html.escape(e.get("title") or "Untitled")}</b>'
            f'<div class="mute">{len(e.get("items", {}))} photos · last checked {html.escape(e.get("synced", "?"))}</div></div>'
            f'<form method="post" action="/sync"><input type="hidden" name="album" value="{aid}">'
            f'<button class="alt">Sync</button></form>'
            f'<form method="post" action="/forget"><input type="hidden" name="album" value="{aid}">'
            f'<button class="alt" title="Stop tracking (keeps photos in Immich)">✕</button></form></div>'
        )
    busy = sync_lock.locked()
    button = ('<button disabled>Checking…</button>' if busy
              else '<button>Check all saved albums for new photos</button>')
    return (f'<div class="card"><b>Saved albums</b><form method="post" action="/sync" class="row">{button}</form>'
            + "".join(rows) + "</div>")


IMMICH_PUBLIC = (os.environ.get("IMMICH_PUBLIC_URL") or IMMICH_URL).rstrip("/")


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode()
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
        if u.path != "/":
            return self.send(404, "not found", "text/plain")
        q = urllib.parse.parse_qs(u.query)
        # Accept ?url= or ?text= (Android share sheets put the link in free text).
        raw = (q.get("url") or q.get("text") or [""])[0]
        m = re.search(r"https://\S+", raw)
        jobs_html, active = render_jobs()
        refresh = '<meta http-equiv="refresh" content="3">' if active or sync_lock.locked() else ""
        self.send(200, PAGE.format(refresh=refresh, prefill=html.escape(m.group(0) if m else ""),
                                   saved=render_saved(), jobs=jobs_html))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        album = (form.get("album") or [""])[0]
        if self.path == "/sync":
            if not sync_lock.locked():
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
