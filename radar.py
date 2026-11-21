"""WastedDubs trending-clip radar.

Every run: pull the last few hours of Twitch clips for one game from the
official Helix API, merge clips of the same moment, score them by how fast
they are gaining views (scaled down for huge streamers), and post the best
new ones to a Discord webhook. State (views seen, what was already alerted)
lives in state.json, committed back to the repo by the workflow.

Env:
  TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET  required (free app at dev.twitch.tv)
  DISCORD_WEBHOOK_URL                     required unless DRY_RUN=1
  GAME_NAME      default "Grand Theft Auto VI"   (GAME_ID overrides the lookup)
  LOOKBACK_HOURS default 6    MIN_VIEWS default 50    TOP_N default 5
  LANGUAGES      default "en" (comma list; "*" or "any" = no filter)
  DRY_RUN=1      print alerts instead of posting
  VISUAL_CHECK   default 1: check each candidate's thumbnail - streamer
                 facecam in a corner ranks lower, letterbox bars = cutscene tag
  VEHICLES       default 1: vehicles first - planes, boats, cars, bikes,
                 helicopters (title keywords + YOLOX image detector) rank x2.0,
                 other clips x0.4, so only a truly viral non-vehicle clip wins
  MODEL_PATH     default models/yolox.onnx (downloaded by the workflow)
Standard library + optional opencv-python-headless (visual check skipped without it).
"""
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HELIX = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
FMT = "%Y-%m-%dT%H:%M:%SZ"
MIN_AGE_H = 0.25          # floor so a 1-minute-old clip can't divide by ~0
SAME_MOMENT_VOD_S = 45    # clips this close in the same VOD are one moment
SAME_MOMENT_TIME_S = 90   # fallback when vod_offset is missing
FACECAM_PENALTY = 0.35    # facecam clips keep 35% of their score (sink, not vanish)
FACECAM_EDGE = 0.30       # a face centred in the outer 30% each side = facecam
VEHICLE_BOOST = 2.0       # vehicle clips (title or image)
NON_VEHICLE = 0.4         # everything else: x5 gap, a viral clip can still win
VEHICLE_THRESHOLD = 0.4   # YOLOX score; tested on real GTA VI frames (6/7 hits, 0 false)
MODEL_PATH = os.environ.get("MODEL_PATH", "models/yolox.onnx")
COCO_VEHICLES = {4: "plane", 8: "boat", 2: "car", 3: "motorbike", 5: "bus", 7: "truck", 6: "train"}
VEHICLE_EMOJI = {"plane": "✈️", "heli": "🚁", "boat": "🚤", "motorbike": "🏍️"}
_VEHICLE_WORDS = (
    r"planes?|airplanes?|jets?|pilots?|flight|flying|landing|heli|helis|helicopters?|choppers?|"
    r"boats?|yachts?|jet ?skis?|ships?|cars?|trucks?|bikes?|motorbikes?|motorcycles?|"
    r"drive|drives|driver|drivers|driving|chase|racing|race|"
    r"avi[oó]n|barco|coche|carro|helic[oó]ptero|moto|"
    r"самол[её]т|вертол[её]т|машина|тачка|лодка|"
    r"flugzeug|auto|boot|hubschrauber")
import re as _re
_VEHICLE_RE = _re.compile(rf"(?<!\w)({_VEHICLE_WORDS})(?!\w)", _re.IGNORECASE)


# ---------------------------------------------------------------- pure logic
def parse_ts(s):
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def time_slices(now, hours, step_hours=1):
    """Contiguous [start, end) windows covering the lookback; Helix ignores seconds."""
    end = now.replace(second=0, microsecond=0)
    out = []
    for i in range(hours, 0, -step_hours):
        a = end - timedelta(hours=i)
        b = end - timedelta(hours=i - step_hours)
        out.append((a.strftime(FMT), b.strftime(FMT)))
    return out


def _same_moment(a, b):
    if a["broadcaster_id"] != b["broadcaster_id"]:
        return False
    if a.get("video_id") and a.get("video_id") == b.get("video_id") \
            and a.get("vod_offset") is not None and b.get("vod_offset") is not None:
        return abs(a["vod_offset"] - b["vod_offset"]) <= SAME_MOMENT_VOD_S
    dt = abs((parse_ts(a["created_at"]) - parse_ts(b["created_at"])).total_seconds())
    return dt <= SAME_MOMENT_TIME_S


def dedupe(clips):
    """Group clips of the same moment; keep the most-viewed, record group size."""
    groups = []
    for c in sorted(clips, key=lambda c: -c["view_count"]):
        for g in groups:
            if _same_moment(g[0], c):
                g.append(c)
                break
        else:
            groups.append([c])
    return [dict(g[0], cluster_size=len(g), members=[c["id"] for c in g]) for g in groups]


def score(c, now, state, viewers):
    """Views/hour since creation, or since the last run if that's higher.
    Divided by sqrt(1 + live_viewers/1000) so big channels don't drown out
    small ones; +15% per extra viewer who clipped the same moment."""
    age_h = max((now - parse_ts(c["created_at"])).total_seconds() / 3600, MIN_AGE_H)
    rate = c["view_count"] / age_h
    recent = 0.0
    prev = state.get(c["id"])
    if prev and prev.get("seen") and "views" in prev:
        dt_h = (now - datetime.fromisoformat(prev["seen"])).total_seconds() / 3600
        if dt_h > 0:
            recent = max(0.0, (c["view_count"] - prev["views"]) / dt_h)
    size = math.sqrt(1 + (viewers or 0) / 1000)
    boost = 1 + 0.15 * (c.get("cluster_size", 1) - 1)
    return {"clip": c, "rate": rate, "recent_rate": recent,
            "score": max(rate, recent) / size * boost}


def select_alerts(scored, state, min_views, top_n, languages):
    keep = [s for s in scored
            if not any(state.get(i, {}).get("alerted")
                       for i in s["clip"].get("members", [s["clip"]["id"]]))
            and s["clip"]["view_count"] >= min_views
            and (not languages or s["clip"].get("language") in languages)]
    return sorted(keep, key=lambda s: -s["score"])[:top_n]


def alerted_ids(alerts):
    """Every clip id of every alerted moment, so no clip of it alerts again."""
    return {i for a in alerts for i in a["clip"].get("members", [a["clip"]["id"]])}


def parse_languages(raw):
    langs = {x.strip() for x in (raw or "").split(",") if x.strip()}
    return None if not langs or langs & {"*", "any"} else langs


def load_state(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def merge_states(remote, ours):
    """Combine two runs' state: newest view count wins, alerted is sticky."""
    out = {}
    for cid in set(remote) | set(ours):
        r, o = remote.get(cid), ours.get(cid)
        if not r or not o:
            out[cid] = dict(r or o)
            continue
        newer = o if o.get("seen", "") >= r.get("seen", "") else r
        e = dict(newer)
        if r.get("alerted") or o.get("alerted"):
            e["alerted"] = True
        out[cid] = e
    return out


def merge_state_files(ours_path, target_path):
    """Merge this run's state (ours_path) into the checked-out remote file."""
    merged = merge_states(load_state(target_path), load_state(ours_path))
    with open(target_path, "w") as f:
        json.dump(merged, f, separators=(",", ":"))


def update_state(state, clips, alerted_ids, now, max_age_h=48):
    cutoff = now - timedelta(hours=max_age_h)
    new = {k: v for k, v in state.items()
           if v.get("seen") and datetime.fromisoformat(v["seen"]) >= cutoff}
    for c in clips:
        entry = dict(new.get(c["id"], {}))
        entry.update(views=c["view_count"], seen=now.isoformat())
        if c["id"] in alerted_ids:
            entry["alerted"] = True
        new[c["id"]] = entry
    return new


def _ago(c, now=None):
    now = now or datetime.now(timezone.utc)
    m = int((now - parse_ts(c["created_at"])).total_seconds() // 60)
    return f"{m} min ago" if m < 120 else f"{m // 60} h ago"


def discord_payload(alerts, game_name):
    embeds = []
    for a in alerts[:10]:
        c = a["clip"]
        lines = [
            f"**{c['broadcaster_name']}** · {c['view_count']:,} views · {_ago(c)}",
            f"Gaining ~{int(max(a['rate'], a['recent_rate'])):,} views/hour",
        ]
        if c.get("cluster_size", 1) > 1:
            lines.append(f"{c['cluster_size']} viewers clipped this moment")
        if a.get("vehicle"):
            how = "spotted in the image" if a.get("vehicle_source") == "image" else "from the title"
            lines.append(f"{VEHICLE_EMOJI.get(a['vehicle'], '🚗')} Vehicle: {a['vehicle']} ({how})")
        if a.get("cutscene"):
            lines.append("🎬 Looks like a cutscene: only dub early, non-story ones")
        if a.get("facecam"):
            lines.append("📷 Facecam spotted (full-height crop usually cuts it)")
        lines.append("Paste the link into Claude with a voice memo to make the Short.")
        e = {"title": c["title"][:250] or "Untitled clip", "url": c["url"],
             "description": "\n".join(lines)}
        if str(c.get("thumbnail_url", "")).startswith("http"):
            e["thumbnail"] = {"url": c["thumbnail_url"]}
        embeds.append(e)
    content = f"🔥 {len(embeds)} trending {game_name} clip(s)"
    return {"content": content[:2000], "embeds": embeds}


# ---------------------------------------------------------------- visual checks
def is_letterboxed(img):
    """Cinematic black bars top AND bottom with a lit picture between them."""
    import numpy as np
    g = img.mean(axis=2) if img.ndim == 3 else img
    h = g.shape[0]
    band = max(2, int(h * 0.08))
    top, bot, mid = g[:band], g[-band:], g[int(h * 0.2):int(h * 0.8)]
    # bars are uniformly black: no streetlights / HUD pixels in them (night scenes)
    dark = lambda a: float(a.mean()) < 14 and float(np.std(a)) < 10 \
        and float(np.percentile(a, 99.5)) < 40
    return bool(dark(top) and dark(bot) and float(mid.mean()) > 30)


_HAAR = None


def _haar_detector(gray):
    global _HAAR
    import cv2
    if _HAAR is None:
        _HAAR = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    return list(_HAAR.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(18, 18)))


def has_facecam(img, detector=None):
    """A real-looking face centred near the left/right edge = webcam overlay.
    Faces in the middle are treated as game characters."""
    import numpy as np
    gray = img.mean(axis=2).astype(np.uint8) if img.ndim == 3 else img
    w = gray.shape[1]
    for (x, y, fw, fh) in (detector or _haar_detector)(gray):
        cx = (x + fw / 2) / w
        if cx < FACECAM_EDGE or cx > 1 - FACECAM_EDGE:
            return True
    return False


def _yolox_anchor_count(size=640):
    return sum((size // st) ** 2 for st in (8, 16, 32))


def yolox_vehicle_scores(out):
    """YOLOX output (1, anchors, 85) -> best objectness x class score per vehicle."""
    d = out[0]
    obj = d[:, 4]
    return {name: float((obj * d[:, 5 + cid]).max()) for cid, name in COCO_VEHICLES.items()}


_NET = None


def _vehicle_net():
    global _NET
    if _NET is None:
        _NET = False
        try:
            import cv2
            if os.path.exists(MODEL_PATH):
                _NET = cv2.dnn.readNet(MODEL_PATH)
        except Exception as e:  # corrupt/partial model: fall back to titles only
            print(f"vehicle detector unavailable ({e.__class__.__name__}); using titles only")
    return _NET or None


def detect_vehicle(img):
    """Letterbox to 640, run YOLOX (OpenCV zoo, Apache-2.0), return label or None."""
    import cv2
    import numpy as np
    net = _vehicle_net()
    if net is None:
        return None
    h, w = img.shape[:2]
    r = min(640 / h, 640 / w)
    canvas = np.full((640, 640, 3), 114, np.uint8)
    canvas[:int(h * r), :int(w * r)] = cv2.resize(img, (int(w * r), int(h * r)))
    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32)
    net.setInput(np.transpose(rgb, (2, 0, 1))[None])
    return vehicle_from_scores(yolox_vehicle_scores(net.forward()))


def analyse_image_bytes(data, detector=None):
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None
    if img is None:
        return None
    a = {"cutscene": is_letterboxed(img), "facecam": has_facecam(img, detector)}
    if os.environ.get("VEHICLES", "1") == "1" and _vehicle_net() is not None:
        try:
            a["vehicle"] = detect_vehicle(img)
        except Exception:
            a["vehicle"] = None
    return a


def apply_visual(scored, analyses):
    out = []
    for s in scored:
        a = analyses.get(s["clip"]["id"])
        s = dict(s)
        if a:
            # in a cutscene, faces near the edges are characters, not a webcam
            s["cutscene"] = a["cutscene"]
            s["facecam"] = a["facecam"] and not a["cutscene"]
            if s["facecam"]:
                s["score"] = s["score"] * FACECAM_PENALTY
        out.append(s)
    return out


def title_vehicle(title):
    m = _VEHICLE_RE.search(title or "")
    return m.group(1).lower() if m else None


def vehicle_from_scores(scores, threshold=VEHICLE_THRESHOLD):
    name, best = max(scores.items(), key=lambda kv: kv[1], default=(None, 0.0))
    return name if best >= threshold else None


def prelim_vehicle_boost(scored):
    """Cheap first pass so title-vehicle clips make it into the checked pool.
    Keeps the real score in base_score for the final ranking."""
    out = []
    for s in scored:
        s = dict(s, base_score=s.get("base_score", s["score"]))
        factor = VEHICLE_BOOST if title_vehicle(s["clip"].get("title")) else NON_VEHICLE
        s["score"] = s["base_score"] * factor
        out.append(s)
    return out


def rank_with_visual(pool, analyses, top_n, vehicles=True):
    """Re-rank only the clips whose thumbnails were checked: facecam penalty,
    then vehicles first (image detector, else title keyword)."""
    base = [dict(s, score=s.get("base_score", s["score"])) for s in pool]
    checked = apply_visual(base, analyses)
    if vehicles:
        for s in checked:
            img = (analyses.get(s["clip"]["id"]) or {}).get("vehicle")
            word = title_vehicle(s["clip"].get("title"))
            if img:
                s["vehicle"], s["vehicle_source"] = img, "image"
            elif word:
                s["vehicle"], s["vehicle_source"] = word, "title"
            s["score"] *= VEHICLE_BOOST if s.get("vehicle") else NON_VEHICLE
    return sorted(checked, key=lambda s: -s["score"])[:top_n]


def visual_check_available():
    """Needs OpenCV 4.x: OpenCV 5 moved the Haar face model out of the main package."""
    try:
        import cv2
        return hasattr(cv2, "CascadeClassifier")
    except Exception:
        return False


# ---------------------------------------------------------------- network
def _http(url, data=None, headers=None, method=None, retry_5xx=True):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                remaining = r.headers.get("Ratelimit-Remaining")
                if remaining is not None and int(remaining) < 5:
                    reset = int(r.headers.get("Ratelimit-Reset", time.time() + 5))
                    time.sleep(max(0, reset - time.time()) + 1)
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            if e.code == 429 or (retry_5xx and e.code >= 500):
                wait = e.headers.get("Retry-After") if e.headers else None
                time.sleep(float(wait) if wait else 2 ** attempt)
                continue
            raise
    raise RuntimeError(f"giving up on {url}")


def get_token(cid, secret):
    data = urllib.parse.urlencode({"client_id": cid, "client_secret": secret,
                                   "grant_type": "client_credentials"}).encode()
    return _http(TOKEN_URL, data=data, method="POST")["access_token"]


def helix(path, params, cid, token):
    q = urllib.parse.urlencode(params, doseq=True)
    return _http(f"{HELIX}/{path}?{q}",
                 headers={"Client-Id": cid, "Authorization": f"Bearer {token}"})


def find_game_id(name, cid, token):
    data = helix("games", {"name": name}, cid, token).get("data", [])
    return data[0]["id"] if data else None


def fetch_clips(game_id, now, hours, cid, token, max_pages=3):
    clips = {}
    for start, end in time_slices(now, hours):
        cursor = None
        for _ in range(max_pages):
            p = {"game_id": game_id, "started_at": start, "ended_at": end, "first": 100}
            if cursor:
                p["after"] = cursor
            r = helix("clips", p, cid, token)
            for c in r.get("data", []):
                clips[c["id"]] = c
            cursor = r.get("pagination", {}).get("cursor")
            if not cursor:
                break
    return list(clips.values())


def fetch_viewers(broadcaster_ids, cid, token):
    out, ids = {}, list(broadcaster_ids)
    for i in range(0, len(ids), 100):
        r = helix("streams", {"user_id": ids[i:i + 100], "first": 100}, cid, token)
        for s in r.get("data", []):
            out[s["user_id"]] = s["viewer_count"]
    return out


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "wasteddubs-radar"})
    with urllib.request.urlopen(req, timeout=6) as r:
        return r.read(5_000_000)


def analyse_thumbnails(candidates, budget_s=90):
    out = {}
    start = time.monotonic()
    for s in candidates:
        if time.monotonic() - start >= budget_s:
            print("  thumbnail time budget used up; ranking the rest unchecked")
            break
        url = str(s["clip"].get("thumbnail_url") or "")
        if not url.startswith("http"):
            continue
        try:
            a = analyse_image_bytes(fetch_bytes(url))
        except Exception as e:  # a bad thumbnail must never stop alerts
            print(f"  thumbnail skipped for {s['clip']['id']}: {e}")
            continue
        if a:
            out[s["clip"]["id"]] = a
    return out


def post_discord(url, payload):
    _http(url, data=json.dumps(payload).encode(),
          headers={"Content-Type": "application/json",
                   "User-Agent": "wasteddubs-radar (github actions)"}, method="POST",
          retry_5xx=False)


# ---------------------------------------------------------------- main
def main():
    cid, secret = os.environ["TWITCH_CLIENT_ID"], os.environ["TWITCH_CLIENT_SECRET"]
    game_name = os.environ.get("GAME_NAME", "Grand Theft Auto VI")
    hours = int(os.environ.get("LOOKBACK_HOURS", "6"))
    min_views = int(os.environ.get("MIN_VIEWS", "50"))
    top_n = int(os.environ.get("TOP_N", "5"))
    langs = parse_languages(os.environ.get("LANGUAGES", "en"))
    dry = os.environ.get("DRY_RUN") == "1"
    now = datetime.now(timezone.utc)

    token = get_token(cid, secret)
    game_id = os.environ.get("GAME_ID") or find_game_id(game_name, cid, token)
    if not game_id:
        print(f"No Twitch category named {game_name!r} yet; nothing to do.")
        return 0
    print(f"{game_name} -> game_id {game_id} | lookback {hours}h, min views {min_views}, "
          f"languages {sorted(langs) if langs else 'any'}, dry run {dry}")

    state = load_state(STATE_FILE)
    raw = fetch_clips(game_id, now, hours, cid, token)
    moments = dedupe(raw)
    viewers = fetch_viewers({c["broadcaster_id"] for c in moments}, cid, token) if moments else {}
    scored = [score(c, now, state, viewers.get(c["broadcaster_id"])) for c in moments]
    visual = os.environ.get("VISUAL_CHECK", "1") == "1" and visual_check_available()
    vehicles =os.environ.get("VEHICLES", "1") == "1"
    if vehicles:
        scored = prelim_vehicle_boost(scored)
    if visual:
        pool = select_alerts(scored, state, min_views, top_n * 6, langs)
        analyses = analyse_thumbnails(pool)
        print(f"thumbnails checked: {len(analyses)}/{len(pool)} | facecam "
              f"{sum(a['facecam'] for a in analyses.values())} | cutscene "
              f"{sum(a['cutscene'] for a in analyses.values())} | vehicle "
              f"{sum(bool(a.get('vehicle')) for a in analyses.values())} "
              f"(detector {'on' if _vehicle_net() is not None else 'off: titles only'})")
    else:
        pool, analyses = select_alerts(scored, state, min_views, top_n * 6, langs), {}
    alerts = rank_with_visual(pool, analyses, top_n, vehicles=vehicles)
    top = sorted(scored, key=lambda x: -x["score"])[:3]
    print(f"{len(raw)} clips -> {len(moments)} moments -> {len(alerts)} alerts")
    for t in top:
        c = t["clip"]
        print(f"  top: {c['view_count']} views, {c.get('language')}, {c['broadcaster_name']}: "
              f"{c['url']}")

    if alerts:
        payload = discord_payload(alerts, game_name)
        if dry:
            print(json.dumps(payload, indent=1))
        else:
            post_discord(os.environ["DISCORD_WEBHOOK_URL"], payload)
    sent = set() if dry else alerted_ids(alerts)
    with open(STATE_FILE, "w") as f:   # 7. keep only what the lookback can see again
        json.dump(update_state(state, raw, sent, now, max_age_h=hours + 2), f,
                  separators=(",", ":"))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--merge-state":
        merge_state_files(sys.argv[2], sys.argv[3])
        sys.exit(0)
    sys.exit(main())
