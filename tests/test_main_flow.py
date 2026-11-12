"""End-to-end run of radar.main() with Twitch + Discord faked (no network)."""
import json
from datetime import datetime, timedelta, timezone

import radar


def _ts(mins_ago):
    t = datetime.now(timezone.utc) - timedelta(minutes=mins_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


CLIPS = [
    {"id": "hot", "url": "https://clips.twitch.tv/hot", "broadcaster_id": "1",
     "broadcaster_name": "Small", "creator_name": "v", "video_id": "v1", "game_id": "99",
     "language": "en", "title": "NPC yeets himself", "view_count": 900,
     "created_at": _ts(30), "thumbnail_url": "", "duration": 20.0, "vod_offset": 100},
    {"id": "dup", "url": "https://clips.twitch.tv/dup", "broadcaster_id": "1",
     "broadcaster_name": "Small", "creator_name": "v2", "video_id": "v1", "game_id": "99",
     "language": "en", "title": "same moment", "view_count": 100,
     "created_at": _ts(29), "thumbnail_url": "", "duration": 20.0, "vod_offset": 110},
    {"id": "es", "url": "https://clips.twitch.tv/es", "broadcaster_id": "2",
     "broadcaster_name": "Otro", "creator_name": "v", "video_id": "v2", "game_id": "99",
     "language": "es", "title": "x", "view_count": 5000,
     "created_at": _ts(20), "thumbnail_url": "", "duration": 20.0, "vod_offset": 5},
]


def run(monkeypatch, tmp_path, dry="0"):
    calls = {"posts": [], "clips_requests": 0}

    def fake_http(url, data=None, headers=None, method=None, retry_5xx=True):
        if url.startswith(radar.TOKEN_URL):
            return {"access_token": "tok"}
        if "/helix/games" in url:
            assert "Client-Id" in headers and headers["Authorization"] == "Bearer tok"
            return {"data": [{"id": "99", "name": "Grand Theft Auto VI"}]}
        if "/helix/clips" in url:
            calls["clips_requests"] += 1
            assert "game_id=99" in url and "first=100" in url
            return {"data": CLIPS, "pagination": {}}
        if "/helix/streams" in url:
            return {"data": [{"user_id": "1", "viewer_count": 300}]}
        if url == "https://discord.test/hook":
            calls["posts"].append(json.loads(data))
            return {}
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(radar, "_http", fake_http)
    monkeypatch.setattr(radar, "STATE_FILE", str(tmp_path / "state.json"))
    for k, v in {"TWITCH_CLIENT_ID": "id", "TWITCH_CLIENT_SECRET": "sec",
                 "DISCORD_WEBHOOK_URL": "https://discord.test/hook", "DRY_RUN": dry,
                 "LOOKBACK_HOURS": "2"}.items():
        monkeypatch.setenv(k, v)
    assert radar.main() == 0
    return calls, json.load(open(tmp_path / "state.json"))


def test_full_run_posts_one_deduped_english_alert_and_saves_state(monkeypatch, tmp_path):
    calls, state = run(monkeypatch, tmp_path)
    assert calls["clips_requests"] == 2                      # one per hour slice
    assert len(calls["posts"]) == 1
    embeds = calls["posts"][0]["embeds"]
    assert [e["url"] for e in embeds] == ["https://clips.twitch.tv/hot"]
    assert "2 viewers clipped" in embeds[0]["description"]
    assert state["hot"]["alerted"] is True and state["dup"]["alerted"] is True
    assert "alerted" not in state["es"]


def test_second_run_does_not_realert(monkeypatch, tmp_path):
    run(monkeypatch, tmp_path)
    calls, _ = run(monkeypatch, tmp_path)
    assert calls["posts"] == []


def test_dry_run_posts_nothing_and_marks_nothing_alerted(monkeypatch, tmp_path):
    calls, state = run(monkeypatch, tmp_path, dry="1")
    assert calls["posts"] == [] and not state["hot"].get("alerted")
