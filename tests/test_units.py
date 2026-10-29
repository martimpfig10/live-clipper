from app.highlights import build_windows, snap
from app.pool import jaccard, overlap_ratio
from app.render import Crop, crop_box
from app.transcribe import merge
from app.captions import clean_hashtags, compose
from app.llm import parse_json


def words(n, step=0.5):
    return [{"word": f"w{i}", "start": i * step, "end": i * step + 0.4} for i in range(n)]


def test_merge_drops_overlap_and_offsets():
    a = {"words": [{"word": "a", "start": 0, "end": 1}, {"word": "dup", "start": 600.5, "end": 601}],
         "segments": [{"start": 0, "end": 1, "text": "a"}]}
    b = {"words": [{"word": "dup", "start": 0.5, "end": 1}, {"word": "b", "start": 2, "end": 3}],
         "segments": [{"start": 0.5, "end": 3, "text": "dup b"}]}
    m = merge([(0, 600, a), (600, 1200, b)])
    assert [w["word"] for w in m["words"]] == ["a", "dup", "b"]
    assert m["words"][1]["start"] == 600.5
    assert m["segments"][1]["start"] == 600.5


def test_snap_to_words_and_limits():
    w = words(400)  # 200s of words
    c = snap({"start": 10.2, "end": 40.1}, w, 15, 75, 200)
    assert c and 9.5 <= c["start"] <= 10.5 and 40 <= c["end"] <= 41.5
    assert snap({"start": 10, "end": 15}, w, 15, 75, 200) is None          # too short
    long = snap({"start": 10, "end": 150}, w, 15, 75, 200)
    assert long and long["end"] - long["start"] <= 75.5                     # trimmed
    assert snap({"start": 50, "end": 40}, w, 15, 75, 200) is None            # inverted


def test_windows_cover_everything():
    segs = [{"start": i * 10, "end": i * 10 + 9, "text": "x"} for i in range(200)]  # ~2000s
    ws = build_windows(segs, 720)
    assert ws[0][0] == 0 and ws[-1][1] == segs[-1]["end"]
    assert all(b[0] < a[1] for a, b in zip(ws, ws[1:]))  # overlapping


def test_crop_box_vertical_source():
    w, h, x, y = crop_box(720, 1280, 1080, 960, Crop())
    assert (w, h) == (720, 640) and x == 0 and y == 320
    w2, h2, x2, y2 = crop_box(720, 1280, 1080, 960, Crop(cx=0.25, cy=0.45, zoom=2))
    assert (w2, h2) == (360, 320) and x2 == 0
    w3, h3, x3, y3 = crop_box(1920, 1080, 1080, 960, Crop())  # horizontal upload
    assert h3 == 1080 and w3 == 1216 and x3 == (1920 - 1216) // 2


def test_dedupe_helpers():
    assert overlap_ratio(0, 30, 10, 40) > 0.6
    assert overlap_ratio(0, 30, 31, 60) == 0
    assert jaccard("ele disse que nunca mais ia voltar", "ele disse que nunca mais ia voltar lá") > 0.8


def test_caption_helpers():
    tags = clean_hashtags(["Portugal", "#brasil", "#Brasil", "humor!"], ["#wazeoficial"], 6)
    assert tags == ["#wazeoficial", "#portugal", "#brasil", "#humor"]
    cap = compose("Olha isto.", tags, "🎥 @wazeoficial")
    assert cap.splitlines()[0] == "Olha isto." and cap.endswith("#humor")


def test_parse_json_variants():
    assert parse_json('{"a": 1}') == {"a": 1}
    assert parse_json('blah ```json\n{"a": 2}\n``` end') == {"a": 2}
    assert parse_json('Here you go: {"clips": []} thanks') == {"clips": []}
