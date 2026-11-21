"""Tests for vehicles-first ranking: title keywords + image detector."""
import numpy as np
import pytest

import radar


# --- title keywords -----------------------------------------------------------
@pytest.mark.parametrize("title,hit", [
    ("How did the heli survive???", "heli"),
    ("Plane crash on day one", "plane"),
    ("one of the better drivers", "drivers"),
    ("STEALTH CAR STEAL LO", "car"),
    ("Boat jump lol", "boat"),
    ("se cayó el helicóptero", "helicóptero"),
    ("угнал самолёт", "самолёт"),
    ("Hubschrauber Absturz", "hubschrauber"),
])
def test_title_keywords_find_vehicle_words(title, hit):
    assert radar.title_vehicle(title) == hit


@pytest.mark.parametrize("title", [
    "ohnePixel tries out the strip club", "scary moment", "Oscar winner", "careful bro",
    "WHAT?!", "", None,
])
def test_title_keywords_ignore_non_vehicle_and_partial_words(title):
    assert radar.title_vehicle(title) is None


# --- detector output -> vehicle label -----------------------------------------
def test_vehicle_from_scores_picks_best_class_over_threshold():
    scores = {"plane": 0.71, "car": 0.30, "boat": 0.12}
    assert radar.vehicle_from_scores(scores, threshold=0.4) == "plane"


def test_vehicle_from_scores_none_below_threshold():
    assert radar.vehicle_from_scores({"car": 0.25, "plane": 0.0}, threshold=0.4) is None


def test_detector_postprocess_reads_yolox_layout():
    # one fake anchor row: cx, cy, w, h, objectness, 80 class scores
    n = radar._yolox_anchor_count()
    out = np.zeros((1, n, 85), np.float32)
    out[0, 5, 4] = 0.9            # objectness
    out[0, 5, 5 + 8] = 0.8        # COCO class 8 = boat
    scores = radar.yolox_vehicle_scores(out)
    assert scores["boat"] == pytest.approx(0.72, abs=1e-3)
    assert scores["plane"] == 0.0


# --- ranking ------------------------------------------------------------------
def _s(cid, score, title="x"):
    return {"clip": {"id": cid, "url": f"https://clips.twitch.tv/{cid}", "title": title,
                     "broadcaster_name": "s", "view_count": 500,
                     "created_at": "2026-11-20T17:30:00Z", "thumbnail_url": "", "cluster_size": 1},
            "score": score, "rate": score, "recent_rate": 0.0}


def test_vehicle_clip_beats_bigger_non_vehicle_clip():
    pool = [_s("club", 100.0), _s("plane", 40.0)]
    analyses = {"club": {"facecam": False, "cutscene": False, "vehicle": None},
                "plane": {"facecam": False, "cutscene": False, "vehicle": "plane"}}
    out = radar.rank_with_visual(pool, analyses, top_n=2)
    assert [s["clip"]["id"] for s in out] == ["plane", "club"]


def test_truly_viral_non_vehicle_clip_still_ranks_first():
    pool = [_s("viral", 1000.0), _s("car", 40.0)]
    analyses = {"viral": {"facecam": False, "cutscene": False, "vehicle": None},
                "car": {"facecam": False, "cutscene": False, "vehicle": "car"}}
    out = radar.rank_with_visual(pool, analyses, top_n=2)
    assert out[0]["clip"]["id"] == "viral"


def test_title_keyword_counts_when_thumbnail_could_not_be_checked():
    pool = [_s("a", 100.0, "lol"), _s("b", 40.0, "heli crash")]
    out = radar.rank_with_visual(pool, {}, top_n=2)
    assert [s["clip"]["id"] for s in out] == ["b", "a"]
    assert out[0]["vehicle"] == "heli" and out[0]["vehicle_source"] == "title"


def test_prelim_boost_pulls_title_vehicle_clips_into_the_checked_pool():
    scored = [_s(str(i), 100.0 - i) for i in range(10)] + [_s("heli", 30.0, "heli moment")]
    boosted = radar.prelim_vehicle_boost(scored)
    top3 = [s["clip"]["id"] for s in sorted(boosted, key=lambda s: -s["score"])[:3]]
    assert "heli" in top3
    # the original score is kept for the final ranking
    assert [s for s in boosted if s["clip"]["id"] == "heli"][0]["base_score"] == 30.0


def test_discord_shows_vehicle_tag():
    s = dict(_s("x", 10.0), vehicle="plane", vehicle_source="image")
    d = radar.discord_payload([s], "G")["embeds"][0]["description"]
    assert "Vehicle: plane" in d


def test_game_crashed_is_not_a_vehicle_title():
    assert radar.title_vehicle("game crashed again") is None


def test_corrupt_model_never_crashes_and_keeps_facecam_check(tmp_path, monkeypatch):
    cv2 = pytest.importorskip("cv2")
    bad = tmp_path / "yolox.onnx"
    bad.write_bytes(b"not a model" * 1000)
    monkeypatch.setattr(radar, "MODEL_PATH", str(bad))
    monkeypatch.setattr(radar, "_NET", None)
    img = np.random.default_rng(0).integers(40, 220, (272, 480, 3), dtype=np.uint8)
    a = radar.analyse_image_bytes(cv2.imencode(".jpg", img)[1].tobytes(), detector=lambda g: [])
    assert a is not None and a["facecam"] is False and not a.get("vehicle")
    assert radar._vehicle_net() is None          # failed load is remembered, not retried


def test_detector_error_keeps_the_rest_of_the_analysis(monkeypatch):
    cv2 = pytest.importorskip("cv2")
    monkeypatch.setattr(radar, "_vehicle_net", lambda: object())
    monkeypatch.setattr(radar, "detect_vehicle", lambda img: (_ for _ in ()).throw(RuntimeError("boom")))
    img = np.random.default_rng(1).integers(40, 220, (272, 480, 3), dtype=np.uint8)
    a = radar.analyse_image_bytes(cv2.imencode(".jpg", img)[1].tobytes(), detector=lambda g: [])
    assert a == {"cutscene": False, "facecam": False, "vehicle": None}
