"""Tests for the thumbnail checks: cutscene (letterbox) tag + facecam rank penalty."""
from datetime import datetime, timezone

import numpy as np
import pytest

import radar

cv2 = pytest.importorskip("cv2")
NOW = datetime(2026, 11, 20, 18, 0, 0, tzinfo=timezone.utc)


def gameplay_frame(h=272, w=480, seed=0):
    """Busy, bright-ish frame with no black bars (stands in for gameplay)."""
    rng = np.random.default_rng(seed)
    return rng.integers(40, 220, size=(h, w, 3), dtype=np.uint8)


def letterboxed_frame(h=272, w=480):
    img = gameplay_frame(h, w)
    bar = int(h * 0.12)
    img[:bar] = 0
    img[-bar:] = 0
    return img


# --- cutscene / letterbox ---------------------------------------------------
def test_letterbox_detected_on_cinematic_bars():
    assert radar.is_letterboxed(letterboxed_frame()) is True


def test_no_letterbox_on_normal_gameplay():
    assert radar.is_letterboxed(gameplay_frame()) is False


def test_no_letterbox_on_fully_black_frame():
    assert radar.is_letterboxed(np.zeros((272, 480, 3), np.uint8)) is False


def test_no_letterbox_when_only_top_is_dark():
    img = gameplay_frame()
    img[:40] = 0
    assert radar.is_letterboxed(img) is False


# --- facecam (corner face) ----------------------------------------------------
def test_face_in_corner_counts_as_facecam():
    img = gameplay_frame()
    fake = lambda gray: [(400, 190, 50, 50)]          # bottom-right corner
    assert radar.has_facecam(img, detector=fake) is True


def test_face_in_middle_is_a_game_character_not_facecam():
    img = gameplay_frame()
    fake = lambda gray: [(215, 100, 50, 50)]          # centre of frame
    assert radar.has_facecam(img, detector=fake) is False


def test_no_faces_no_facecam():
    assert radar.has_facecam(gameplay_frame(), detector=lambda g: []) is False


def test_real_opencv_detector_runs_and_finds_nothing_in_noise():
    # smoke test: the bundled Haar model loads and runs without a download
    assert radar.has_facecam(gameplay_frame()) is False


def test_analyse_image_bytes_round_trip():
    ok, buf = cv2.imencode(".jpg", letterboxed_frame())
    a = radar.analyse_image_bytes(buf.tobytes(), detector=lambda g: [])
    assert a == {"cutscene": True, "facecam": False}
    assert radar.analyse_image_bytes(b"not an image") is None


# --- ranking + alert text ----------------------------------------------------
def _s(cid, score):
    return {"clip": {"id": cid, "url": f"https://clips.twitch.tv/{cid}",
                     "broadcaster_name": "s", "view_count": 500, "title": cid,
                     "created_at": "2026-11-20T17:30:00Z", "thumbnail_url": "",
                     "cluster_size": 1},
            "score": score, "rate": score, "recent_rate": 0.0}


def test_facecam_clip_ranks_below_similar_clean_clip_but_still_alerts_if_huge():
    scored = [_s("cam", 100.0), _s("clean", 80.0), _s("hugecam", 1000.0)]
    analyses = {"cam": {"facecam": True, "cutscene": False},
                "clean": {"facecam": False, "cutscene": False},
                "hugecam": {"facecam": True, "cutscene": False}}
    out = radar.apply_visual(scored, analyses)
    order = [s["clip"]["id"] for s in sorted(out, key=lambda s: -s["score"])]
    assert order == ["hugecam", "clean", "cam"]


def test_unanalysed_clip_keeps_its_score():
    out = radar.apply_visual([_s("x", 50.0)], {})
    assert out[0]["score"] == 50.0 and not out[0].get("facecam")


def test_discord_shows_cutscene_and_facecam_tags():
    s = dict(_s("x", 10.0), cutscene=True, facecam=True)
    d = radar.discord_payload([s], "G")["embeds"][0]["description"]
    assert "🎬" in d and "early, non-story" in d
    assert "📷" in d
    plain = radar.discord_payload([_s("y", 10.0)], "G")["embeds"][0]["description"]
    assert "🎬" not in plain and "📷" not in plain


# --- regressions from independent review -------------------------------------
def test_final_alerts_come_only_from_checked_clips():
    pool = [_s("cam1", 100.0), _s("cam2", 90.0), _s("clean", 60.0)]
    analyses = {"cam1": {"facecam": True, "cutscene": False},
                "cam2": {"facecam": True, "cutscene": False},
                "clean": {"facecam": False, "cutscene": False}}
    out = radar.rank_with_visual(pool, analyses, top_n=2)
    assert [s["clip"]["id"] for s in out] == ["clean", "cam1"]


def test_missing_thumbnail_url_does_not_crash(monkeypatch):
    c = _s("x", 1.0)
    c["clip"]["thumbnail_url"] = None
    monkeypatch.setattr(radar, "fetch_bytes", lambda u: (_ for _ in ()).throw(AssertionError))
    assert radar.analyse_thumbnails([c]) == {}


def test_thumbnail_checks_stop_at_time_budget(monkeypatch):
    clips = [dict(_s(str(i), 1.0)) for i in range(5)]
    for c in clips:
        c["clip"]["thumbnail_url"] = "https://x/t.jpg"
    t = {"now": 0.0}
    monkeypatch.setattr(radar.time, "monotonic", lambda: t["now"])

    def slow(url):
        t["now"] += 30.0
        ok, buf = cv2.imencode(".jpg", gameplay_frame())
        return buf.tobytes()
    monkeypatch.setattr(radar, "fetch_bytes", slow)
    got = radar.analyse_thumbnails(clips, budget_s=60)
    assert len(got) == 2


def test_cutscene_close_up_is_not_penalised_as_facecam():
    out = radar.apply_visual([_s("x", 100.0)], {"x": {"facecam": True, "cutscene": True}})
    assert out[0]["score"] == 100.0 and out[0]["cutscene"] and not out[0]["facecam"]


def test_night_scene_with_streetlights_is_not_a_cutscene():
    img = gameplay_frame()
    bar = int(272 * 0.12)
    img[:bar] = 6
    img[-bar:] = 6
    rng = np.random.default_rng(3)
    for _ in range(60):                       # scattered lights / HUD bits
        y = int(rng.integers(0, bar)); x = int(rng.integers(0, 476))
        img[y:y + 3, x:x + 3] = 230
        y = int(rng.integers(272 - bar, 269)); img[y:y + 3, x:x + 3] = 230
    assert radar.is_letterboxed(img) is False
