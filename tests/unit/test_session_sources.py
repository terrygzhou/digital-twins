"""Session-store sources: pi reads a synthetic store; dsh requires its knob."""

import json
import time

from digital_twins.sources import build


def _pi_store(tmp_path):
    proj = tmp_path / "demo"
    proj.mkdir()
    now = int(time.time() * 1000)
    lines = [{"type": "session", "id": "s1", "cwd": "/x/demo",
              "timestamp": now}]
    for n in range(4):
        lines.append({"type": "message", "message": {
            "role": "user" if n % 2 == 0 else "assistant",
            "content": f"do thing {n}",
            "timestamp": now + n * 1000,
        }})
    (proj / "s1.jsonl").write_text("\n".join(json.dumps(l) for l in lines))
    return tmp_path


def test_pi_source_reads_store_and_filters_trivial(tmp_path):
    store = _pi_store(tmp_path)
    # one trivial session below min_messages
    trivial = store / "triv" / "s2.jsonl"
    trivial.parent.mkdir()
    trivial.write_text(json.dumps(
        {"type": "session", "id": "s2", "timestamp": int(time.time() * 1000)}))

    source = build("pi", {"extra": {"sessions_dir": str(store),
                                    "min_messages": 2}})
    assert source.prerequisites() == []
    items = list(source.read(None))
    assert [getattr(i, "key") for i in items] == ["s1"]
    assert "User: do thing 0" in items[0].content
    assert items[0].metadata["project"] == "demo"


def test_pi_source_missing_dir_is_a_prerequisite(tmp_path):
    source = build("pi", {"extra": {"sessions_dir": str(tmp_path / "nope")}})
    assert len(source.prerequisites()) == 1
    assert "sessions_dir" in source.prerequisites()[0]


def test_dsh_source_requires_sessions_dir_knob():
    source = build("dsh", {})
    assert source.prerequisites()
    assert "sources.dsh.extra.sessions_dir" in source.prerequisites()[0]
    assert list(source.read(None)) == []
