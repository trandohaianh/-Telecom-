import csv
import json
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from feature_store import CALL_FEATURES
from shadow_feedback import build_feedback, export_review


def fixture_files(tmp_path, crossing=False):
    database = tmp_path / "journal.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE events(seq INTEGER,kind TEXT,payload TEXT,result TEXT)")
    reviews = []
    for i, ts in enumerate((1, 2, 11, 12, 21, 22)):
        event = dict(event_id=f"event{i}", event_type="CALL", call_id=f"call{i}", ts=ts,
                     caller="PRIVATE_SUBSCRIBER")
        result = dict(decision="ALLOW", ingest="ADMITTED", scored=True,
                      evidence_features={name: 1.0 for name in CALL_FEATURES})
        connection.execute("INSERT INTO events VALUES (?,?,?,?)",
                           (i, "VERDICT", json.dumps(event), json.dumps(result)))
        reviews.append(dict(event_id=f"event{i}", group_id="cross" if crossing and i in (0, 2) else f"group{i}",
                            label=str(i % 2), reviewer="REVIEWER_PRIVATE"))
    connection.commit()
    connection.close()
    labels = tmp_path / "labels.csv"
    with labels.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(reviews[0]))
        writer.writeheader()
        writer.writerows(reviews)
    return database, labels


def test_feedback_export_and_temporal_group_holdout_do_not_leak_identities(tmp_path):
    database, labels = fixture_files(tmp_path, crossing=True)
    review = tmp_path / "review.csv"
    assert export_review(database, review)["review_rows"] == 6
    assert "PRIVATE_SUBSCRIBER" not in review.read_text()
    destination = tmp_path / "feedback"
    report = build_feedback(database, labels, destination, key=b"k" * 32,
                            train_before=10, calibration_before=20)
    assert report["exclusions"]["group_crosses_temporal_boundary"] == 2
    assert report["splits"]["test"]["events"] == 2
    for path in destination.glob("*.jsonl"):
        content = path.read_text()
        assert "PRIVATE_SUBSCRIBER" not in content and "REVIEWER_PRIVATE" not in content
    with pytest.raises(FileExistsError):
        build_feedback(database, labels, destination, key=b"k" * 32,
                       train_before=10, calibration_before=20)


def test_missing_review_observation_and_invalid_cutoffs_fail(tmp_path):
    database, labels = fixture_files(tmp_path)
    with pytest.raises(ValueError, match="cutoff"):
        build_feedback(database, labels, tmp_path / "bad", key=b"k" * 32,
                       train_before=30, calibration_before=20)
    with labels.open("a", encoding="utf-8") as stream:
        stream.write("missing,g,1,reviewer\n")
    with pytest.raises(ValueError, match="absent"):
        build_feedback(database, labels, tmp_path / "bad2", key=b"k" * 32,
                       train_before=10, calibration_before=20)
