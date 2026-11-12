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
Standard library only.
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
        lines.append("Paste the link into Claude with a voice memo to make the Short.")
        e = {"title": c["title"][:250] or "Untitled clip", "url": c["url"],
             "description": "\n".join(lines)}
        if str(c.get("thumbnail_url", "")).startswith("http"):
            e["thumbnail"] = {"url": c["thumbnail_url"]}
        embeds.append(e)
    content = f"🔥 {len(embeds)} trending {game_name} clip(s)"
    return {"content": content[:2000], "embeds": embeds}


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
    alerts = select_alerts(scored, state, min_views, top_n, langs)
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
