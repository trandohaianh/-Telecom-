import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from data_generator import TrafficSimulator
from events_io import load_events


def test_generated_audio_has_unique_call_session_and_valid_timing():
    simulator = TrafficSimulator(seed=732, n_personal=40, n_callcentres=1)
    simulator.legit(0, 3600)
    simulator.deepfake(0, 3600, 10)
    simulator.voice_harvest(0, 3600, 2, (60, 100))
    calls = {e["call_id"]: e for e in simulator.events if e["event_type"] == "CALL"}
    assert len(calls) == sum(e["event_type"] == "CALL" for e in simulator.events)
    audio = [e for e in simulator.events if e["event_type"] == "AUDIO"]
    assert audio
    for segment in audio:
        call = calls[segment["call_id"]]
        assert (segment["caller"], segment["callee"]) == (call["caller"], call["callee"])
        assert call["ts"] <= segment["ts"] <= call["ts"] + call["duration_s"]


def test_csv_loader_preserves_numeric_looking_call_id(tmp_path):
    path = tmp_path / "sessions.csv"
    path.write_text("event_id,event_type,ts,call_id\ne1,CALL,1,0000123\n", encoding="utf-8")
    assert load_events(path)[0]["call_id"] == "0000123"
