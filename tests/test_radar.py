"""Tests for the WastedDubs trending-clip radar (pure logic only; no network)."""
from datetime import datetime, timedelta, timezone

import radar

NOW = datetime(2026, 11, 20, 18, 0, 0, tzinfo=timezone.utc)


def clip(cid, views, mins_ago, bid="b1", vod="v1", off=100, lang="en", name="streamer"):
    return {
        "id": cid, "url": f"https://clips.twitch.tv/{cid}", "broadcaster_id": bid,
        "broadcaster_name": name, "creator_name": "viewer", "video_id": vod,
        "game_id": "g", "language": lang, "title": f"title {cid}", "view_count": views,
        "created_at": (NOW - timedelta(minutes=mins_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "thumbnail_url": "https://example.com/t.jpg", "duration": 30.0, "vod_offset": off,
    }


# --- time slicing -----------------------------------------------------------
def test_time_slices_cover_lookback_in_one_hour_steps_with_zero_seconds():
    s = radar.time_slices(NOW.replace(second=37), hours=3)
    assert len(s) == 3
    assert s[0][0] == "2026-11-20T15:00:00Z" and s[-1][1] == "2026-11-20T18:00:00Z"
    for a, b in s:
        assert a.endswith(":00Z") and b.endswith(":00Z")
    # contiguous, no gaps
    assert all(s[i][1] == s[i + 1][0] for i in range(len(s) - 1))


# --- dedupe -----------------------------------------------------------------
def test_dedupe_merges_same_moment_and_keeps_most_viewed():
    a = clip("a", 40, 30, off=100)
    b = clip("b", 90, 29, off=130)      # same VOD, 30 s apart -> same moment
    c = clip("c", 10, 20, off=400)      # same VOD, far away -> different moment
    out = radar.dedupe([a, b, c])
    ids = {x["id"]: x for x in out}
    assert set(ids) == {"b", "c"}
    assert ids["b"]["cluster_size"] == 2 and ids["c"]["cluster_size"] == 1


def test_dedupe_falls_back_to_created_time_when_vod_offset_missing():
    a = clip("a", 40, 30, vod="", off=None)
    b = clip("b", 60, 29, vod="", off=None)   # 60 s apart -> same moment
    c = clip("c", 70, 10, vod="", off=None)   # 20 min later -> different
    assert {x["id"] for x in radar.dedupe([a, b, c])} == {"b", "c"}


def test_dedupe_never_merges_different_streamers():
    a = clip("a", 40, 30, bid="b1")
    b = clip("b", 60, 30, bid="b2")
    assert len(radar.dedupe([a, b])) == 2


# --- scoring ----------------------------------------------------------------
def test_score_prefers_fast_growth_over_raw_views():
    old_big = clip("old", 3000, 300)    # 5 h old, 600 views/h
    new_hot = clip("new", 800, 30)      # 30 min old, 1600 views/h
    s_old = radar.score(dict(old_big, cluster_size=1), NOW, {}, viewers=None)
    s_new = radar.score(dict(new_hot, cluster_size=1), NOW, {}, viewers=None)
    assert s_new["score"] > s_old["score"]


def test_score_uses_recent_gain_from_previous_run():
    c = dict(clip("x", 1000, 120), cluster_size=1)
    prev = {"x": {"views": 400, "seen": (NOW - timedelta(minutes=10)).isoformat()}}
    s = radar.score(c, NOW, prev, viewers=None)
    assert round(s["recent_rate"]) == 3600          # +600 views in 10 min
    assert s["score"] >= s["recent_rate"] / 1.0001  # recent rate dominates


def test_score_small_streamer_beats_big_streamer_at_same_rate():
    c = dict(clip("x", 500, 30), cluster_size=1)
    small = radar.score(c, NOW, {}, viewers=200)["score"]
    big = radar.score(c, NOW, {}, viewers=80000)["score"]
    assert small > big


def test_score_boosts_moments_clipped_by_many_viewers():
    one = radar.score(dict(clip("x", 500, 30), cluster_size=1), NOW, {}, None)["score"]
    five = radar.score(dict(clip("x", 500, 30), cluster_size=5), NOW, {}, None)["score"]
    assert five > one


def test_score_age_floor_prevents_division_blowup():
    s = radar.score(dict(clip("x", 50, 0), cluster_size=1), NOW, {}, None)
    assert s["rate"] <= 50 / 0.25 + 1e-6


# --- selection + state ------------------------------------------------------
def test_select_alerts_skips_already_alerted_low_views_and_wrong_language():
    scored = [
        {"clip": clip("a", 900, 20), "score": 50.0},
        {"clip": clip("b", 900, 20), "score": 90.0},
        {"clip": clip("c", 10, 5), "score": 999.0},             # below min views
        {"clip": clip("d", 900, 20, lang="es"), "score": 80.0},  # wrong language
    ]
    state = {"b": {"alerted": True}}
    out = radar.select_alerts(scored, state, min_views=50, top_n=5, languages={"en"})
    assert [x["clip"]["id"] for x in out] == ["a"]


def test_select_alerts_respects_top_n_and_orders_by_score():
    scored = [{"clip": clip(str(i), 500, 20), "score": float(i)} for i in range(10)]
    out = radar.select_alerts(scored, {}, min_views=1, top_n=3, languages=None)
    assert [x["clip"]["id"] for x in out] == ["9", "8", "7"]


def test_update_state_records_views_marks_alerts_and_prunes_old():
    state = {"stale": {"views": 1, "seen": (NOW - timedelta(hours=60)).isoformat()}}
    clips = [clip("a", 100, 10), clip("b", 200, 10)]
    new = radar.update_state(state, clips, alerted_ids={"b"}, now=NOW, max_age_h=48)
    assert "stale" not in new
    assert new["a"]["views"] == 100 and not new["a"].get("alerted")
    assert new["b"]["alerted"] is True


# --- Discord payload --------------------------------------------------------
def test_discord_payload_has_one_embed_per_alert_with_link_and_stats():
    alerts = [{"clip": dict(clip("a", 1234, 30), cluster_size=3), "score": 10.0,
               "rate": 2468.0, "recent_rate": 0.0}]
    p = radar.discord_payload(alerts, "Grand Theft Auto VI")
    assert len(p["embeds"]) == 1
    e = p["embeds"][0]
    assert e["url"] == "https://clips.twitch.tv/a"
    assert "1,234" in e["description"] and "streamer" in e["description"]
    assert "3 viewers clipped" in e["description"]
    assert len(p["content"]) <= 2000


def test_discord_payload_caps_at_ten_embeds():
    alerts = [{"clip": dict(clip(str(i), 100, 10), cluster_size=1), "score": 1.0,
               "rate": 1.0, "recent_rate": 0.0} for i in range(15)]
    assert len(radar.discord_payload(alerts, "G")["embeds"]) == 10


# --- regressions from independent review -------------------------------------
def test_same_moment_never_alerts_twice_when_another_clip_overtakes():
    a, b = clip("a", 900, 30, off=100), clip("b", 100, 29, off=110)
    m1 = radar.dedupe([a, b])
    assert sorted(m1[0]["members"]) == ["a", "b"]
    sent = radar.alerted_ids(radar.select_alerts(
        [radar.score(m, NOW, {}, None) for m in m1], {}, 1, 5, None))
    state = radar.update_state({}, [a, b], sent, NOW)
    # next run: b overtakes a and becomes the representative clip
    a2, b2 = clip("a", 950, 40, off=100), clip("b", 5000, 39, off=110)
    m2 = radar.dedupe([a2, b2])
    assert m2[0]["id"] == "b"
    out = radar.select_alerts([radar.score(m, NOW, state, None) for m in m2], state, 1, 5, None)
    assert out == []


def test_discord_payload_omits_missing_thumbnail():
    c = dict(clip("a", 100, 10), cluster_size=1, thumbnail_url="")
    e = radar.discord_payload([{"clip": c, "score": 1, "rate": 1, "recent_rate": 0}], "G")["embeds"][0]
    assert "thumbnail" not in e


def test_language_setting_any_means_no_filter():
    assert radar.parse_languages("*") is None
    assert radar.parse_languages("any") is None
    assert radar.parse_languages("") is None
    assert radar.parse_languages("en, es") == {"en", "es"}


def test_corrupt_state_file_is_treated_as_empty(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("")
    assert radar.load_state(str(p)) == {}
    p.write_text("{not json")
    assert radar.load_state(str(p)) == {}
