"""Malformed CALL quarantine and Spark checkpoint ownership regressions."""
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from distributed_stream import DistributedCallStream, classify_record
from event_contract import EVENT_FIELDS, validate_call
from stateful_stream import StatefulCallStream
from test_distributed_stream import Record
from test_stateful_stream import SOURCE, event


class CallContractTests(unittest.TestCase):
    def test_numeric_overflow_is_a_validation_error(self):
        for key in ("ts", "caller_age_days", "duration_s"):
            for value in (10 ** 1000, float("inf"), float("nan"), True, "1", None):
                with self.subTest(key=key, value_type=type(value).__name__):
                    with self.assertRaisesRegex(ValueError, "finite number"):
                        validate_call(event(**{key: value}))

    def test_completion_overflow_and_huge_integer_are_classified(self):
        rows = [event("overflow", 1e308, duration_s=1e308),
                event("huge", 10 ** 1000)]
        for row in rows:
            classified = classify_record(Record(row))
            self.assertEqual(len(classified), len(EVENT_FIELDS) + 4)
            self.assertEqual(classified[0], row["event_id"])
            self.assertIsNotNone(classified[-3])
            self.assertFalse(classified[-2])
            self.assertEqual(json.loads(classified[-1])["event_id"], row["event_id"])
        # D12 (owner decision 17/11/2026): far-future/ms timestamps are rejected like the main API.
        with self.assertRaisesRegex(ValueError, "Unix seconds"):
            validate_call(event("finite", 1e308, duration_s=0.))
        with self.assertRaisesRegex(ValueError, "Unix seconds"):
            validate_call(event("millis", 1.7e12))
        self.assertEqual(validate_call(event("unix", 1.7e9))["ts"], 1.7e9)

    def test_live_completion_fields_cannot_override_replay(self):
        for key in ("start_ts", "end_ts"):
            for value in (0., float("inf"), "bad"):
                with self.assertRaisesRegex(ValueError, r"ts \+ duration_s; unsupported fields"):
                    validate_call(event(**{key: value}))
            # pandas inserts nulls into unaffected rows of a mixed-schema frame.
            for missing in (None, float("nan")):
                normalized = validate_call(event(**{key: missing}))
                self.assertNotIn(key, normalized)
            # Fixed Spark schema may omit even an explicitly null JSON field.
            for value in (0., None):
                envelope = event(**{key: value})
                parsed = event(_raw=json.dumps(envelope))
                classified = classify_record(Record(parsed))
                self.assertIn("unsupported fields: " + key, classified[-3])

    def test_malformed_records_do_not_poison_local_batch(self):
        rows = [event("overflow", 1e308, duration_s=1e308),
                event("huge", 10 ** 1000), event("live-end", 100., end_ts=101.),
                event("live-start", 100., _raw=json.dumps(event(start_ts=99.))),
                event("valid", 100.)]
        with tempfile.TemporaryDirectory() as directory:
            processor = StatefulCallStream(Path(directory), SOURCE)
            result = processor.process(pd.DataFrame(rows, dtype=object), 0)
            self.assertEqual(result.event_id.tolist(), ["valid"])
            self.assertEqual(processor.last_metrics["quarantined"], 4)
            self.assertEqual(processor.last_metrics["accepted_rows"], 1)
            self.assertEqual(processor.max_ts, 100.)
            restarted = StatefulCallStream(Path(directory), SOURCE)
            next_result = restarted.process(pd.DataFrame([event("next", 101.)]), 1)
            self.assertEqual(next_result.iloc[0].calls_1m, 2.)


class CheckpointOwnerTests(unittest.TestCase):
    ENGINES = (("call_features_v2", lambda path: StatefulCallStream(path, SOURCE)),
               ("distributed_call_features_v1", lambda path: DistributedCallStream(None, path, SOURCE)))

    def test_foreign_and_unknown_feature_namespaces_are_rejected(self):
        for own, factory in self.ENGINES:
            for name in ("call_features_v1", "call_features_v2", "call_features_v999",
                         "distributed_call_features_v1", "distributed_call_features_v999"):
                if name == own:
                    continue
                with self.subTest(own=own, foreign=name), tempfile.TemporaryDirectory() as directory:
                    checkpoint = Path(directory)
                    (checkpoint / name).mkdir()
                    with self.assertRaisesRegex(ValueError, "fresh checkpoint"):
                        factory(checkpoint)
                    self.assertFalse((checkpoint / own).exists())

    def test_orphan_spark_progress_is_rejected_before_manifest_write(self):
        for own, factory in self.ENGINES:
            for marker in ("offsets", "commits", "sources", "state", "metadata"):
                for empty_namespace in (False, True):
                    with self.subTest(own=own, marker=marker, empty=empty_namespace):
                        with tempfile.TemporaryDirectory() as directory:
                            checkpoint = Path(directory)
                            if marker == "metadata":
                                (checkpoint / marker).write_text("{}", encoding="utf-8")
                            else:
                                (checkpoint / marker).mkdir()
                            if empty_namespace:
                                (checkpoint / own).mkdir()
                            with self.assertRaisesRegex(ValueError, "no matching feature manifest"):
                                factory(checkpoint)
                            self.assertFalse((checkpoint / own / "source.json").exists())

    def test_own_manifest_allows_normal_spark_restart(self):
        for own, factory in self.ENGINES:
            with self.subTest(own=own), tempfile.TemporaryDirectory() as directory:
                checkpoint = Path(directory)
                processor = factory(checkpoint)
                if own == "call_features_v2":
                    processor.process(pd.DataFrame([event("first", 0.)]), 0)
                (checkpoint / "offsets").mkdir()
                restarted = factory(checkpoint)
                if own == "call_features_v2":
                    result = restarted.process(pd.DataFrame([event("second", 1.)]), 1)
                    self.assertEqual(result.iloc[0].calls_1m, 2.)
                self.assertEqual(restarted.root.name, own)

    def test_foreign_namespace_is_rejected_even_when_own_manifest_exists(self):
        for own, factory in self.ENGINES:
            with self.subTest(own=own), tempfile.TemporaryDirectory() as directory:
                checkpoint = Path(directory)
                factory(checkpoint)
                other = "call_features_v2" if own.startswith("distributed") else "distributed_call_features_v1"
                (checkpoint / other).mkdir()
                with self.assertRaisesRegex(ValueError, "different feature engine"):
                    factory(checkpoint)


class ImeiPartitionKeyTests(unittest.TestCase):
    def test_rows_without_imei_do_not_share_one_hot_key(self):
        from distributed_features import imei_entity, imei_partition
        rows = [dict(event_id=f"e{i}", caller=f"C{i}", ts=float(i), imei="") for i in range(3)]
        keys = {imei_entity(r) for r in rows}
        self.assertEqual(len(keys), 3)
        self.assertNotIn("", keys)
        out = list(imei_partition(sorted(((imei_entity(r), r["ts"], r["event_id"]), r) for r in rows)))
        self.assertEqual([v for _, v in out], [0.0, 0.0, 0.0])
        same = [dict(event_id="a", caller="X", ts=0.0, imei="35391"), dict(event_id="b", caller="Y", ts=1.0, imei="35391")]
        self.assertEqual(imei_entity(same[0]), "35391")


if __name__ == "__main__":
    unittest.main()
