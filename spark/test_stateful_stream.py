"""Local state, input contract and replay tests; no Kafka/Spark service required."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from compare_features import compare
from distributed_features import cell_partition, event_order, imei_partition, subscriber_partition, subscriber_records
from produce_to_kafka import replay
from stateful_stream import CALL_FEATURES, StatefulCallStream

from feature_store import FeatureStore

SOURCE = {"bootstrap": "test", "topic": "ordered-v2"}


def event(event_id="a", ts=0., caller="A", callee="B", **changes):
    row = dict(event_id=event_id, event_type="CALL", ts=ts, caller=caller, callee=callee,
               imei="353911100000001", cell_id="X-test", origin_trunk="LOCAL",
               network="4G", caller_age_days=100., duration_s=1.)
    row.update(changes)
    return row


def reference(rows):
    store, output = FeatureStore(), []
    for row in sorted(rows, key=event_order):
        output.append({"event_id": row["event_id"], **store.call_features(row)})
        store.commit_cdr(row)
    return pd.DataFrame(output).set_index("event_id")[CALL_FEATURES].astype(float).sort_index()


class ComparatorTests(unittest.TestCase):
    def test_strict_completeness_and_nonfinite_values(self):
        good = reference([event("a"), event("b", 1.)])
        cases = [good.iloc[:1], good.assign(calls_1m=float("nan")),
                 good.assign(calls_1m=float("inf")), good.drop(columns=["calls_1m"]),
                 pd.concat([good, good.iloc[:1]]),
                 pd.concat([good, good.iloc[:1].rename(index={"a": "extra"})])]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(compare(good, good), 0)
            for bad in cases:
                self.assertEqual(compare(good, bad), 1)
            self.assertEqual(compare(good, good.drop(columns=["calls_1m"]), allow_partial=True), 0)
            self.assertEqual(compare(good, good.iloc[:1], allow_partial=True), 1)


class DistributedKernelTests(unittest.TestCase):
    def test_all_27_cross_key_ties_completion_and_exact_boundaries(self):
        from data_generator import REGISTERED_CELLS
        cell = next(iter(REGISTERED_CELLS))
        rows = [event("a", 0., duration_s=100., cell_id=cell),
                event("b", 50., duration_s=3., cell_id=cell),
                event("c", 60., caller="B", callee="A", cell_id=cell),
                event("d", 60., caller="A", callee="C", cell_id=cell),
                event("e", 60., caller="A", callee="A", imei=""),  # self-call and tied time
                event("f", 3600., caller="C", callee="A", cell_id=cell, caller_age_days=2.),
                event("g", 3600.001, caller="A", callee="B", cell_id=cell),
                event("h", 86400., caller="A", callee="B"),
                event("i", 86400.001, caller="B", callee="A"),
                event("j", 86459., caller="A", callee="B", imei="999999990000000")]
        outgoing = sorted([item for row in rows for item in subscriber_records(row)], key=lambda item: item[0])
        core = {row[0]: dict(zip(CALL_FEATURES, row[1:])) for row in subscriber_partition(iter(outgoing))}
        imei_rows = sorted([((row.get("imei") or "", *event_order(row)), row) for row in rows],
                           key=lambda item: item[0])
        for event_id, value in imei_partition(iter(imei_rows)):
            core[event_id]["msisdn_per_imei_24h"] = value
        enriched = [{**row, "active_hours_on_network": core[row["event_id"]]["active_hours_on_network"]}
                    for row in rows]
        cell_rows = sorted([((row["cell_id"], *event_order(row)), row) for row in enriched],
                           key=lambda item: item[0])
        for event_id, value in cell_partition(iter(cell_rows)):
            core[event_id]["new_sims_on_cell_1h"] = value
        actual = pd.DataFrame.from_dict(core, orient="index")[CALL_FEATURES].sort_index()
        actual.index.name = "event_id"
        pd.testing.assert_frame_equal(actual, reference(rows), atol=1e-9, rtol=1e-9)


class StreamStateTests(unittest.TestCase):
    def test_all_features_across_batches_restarts_and_retry(self):
        rows = [event(str(index), float(index), caller="A" if index % 2 else "B",
                      callee="B" if index % 2 else "A", duration_s=float(index % 5))
                for index in range(80)]
        with tempfile.TemporaryDirectory() as directory:
            output = []
            for batch_id, start in enumerate(range(0, len(rows), 13)):
                processor = StatefulCallStream(Path(directory), SOURCE, retain_snapshots=2)
                frame = pd.DataFrame(rows[start:start + 13])
                result = processor.process(frame, batch_id)
                before = json.dumps(processor.store.state_dict(), sort_keys=True)
                pd.testing.assert_frame_equal(result, processor.process(frame, batch_id))
                self.assertEqual(before, json.dumps(processor.store.state_dict(), sort_keys=True))
                output.append(result)
            actual = pd.concat(output).set_index("event_id")[CALL_FEATURES].sort_index()
            pd.testing.assert_frame_equal(actual, reference(rows), atol=1e-9, rtol=1e-9)
            self.assertEqual(len(list(processor.root.glob("batch-*/state.json"))), 2)
            self.assertEqual(len(StatefulCallStream.output_files(directory)), 7)
            # Old retry receipts/output remain valid after full snapshot expiration.
            pd.testing.assert_frame_equal(output[0], processor.process(pd.DataFrame(rows[:13]), 0))

    def test_reorder_buffer_survives_restart_and_flush(self):
        rows = [event("a", 100.), event("b", 105.), event("c", 112.), event("d", 120.)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            processor = StatefulCallStream(path, SOURCE, allowed_lateness_s=10)
            first = processor.process(pd.DataFrame([rows[0], rows[2]]), 0)
            self.assertEqual(first.event_id.tolist(), ["a"])
            processor = StatefulCallStream(path, SOURCE, allowed_lateness_s=10)
            second = processor.process(pd.DataFrame([rows[1], rows[3]]), 1)
            tail = processor.flush()
            actual = pd.concat([first, second, tail]).set_index("event_id")[CALL_FEATURES].sort_index()
            pd.testing.assert_frame_equal(actual, reference(rows), atol=1e-9, rtol=1e-9)
            # Final drain uses its own namespace; the next Spark batch ID remains available.
            restarted = StatefulCallStream(path, SOURCE, allowed_lateness_s=10)
            restarted.process(pd.DataFrame([event("e", 140.)]), 2)
            pd.testing.assert_frame_equal(tail, processor.flush())

    def test_duplicate_conflict_bad_json_and_late_quarantine(self):
        with tempfile.TemporaryDirectory() as directory:
            processor = StatefulCallStream(Path(directory), SOURCE)
            row = event("a", 100.)
            processor.process(pd.DataFrame([row]), 0)
            processor.process(pd.DataFrame([row, {**row, "duration_s": 7.},
                event("late", 1.), {"_parse_error": "bad JSON", "_raw": "{"},
                {"event_type": "SMS"}, event(123, 200.), event("badimei", 200., imei=123)]), 1)
            metrics = processor.last_metrics
            # D13: both in-batch copies of "a" conflict, so both are quarantined (as in the distributed engine).
            self.assertEqual(metrics["duplicates"], 0)
            self.assertEqual(metrics["quarantined"], 6)
            self.assertEqual(metrics["ignored_rows"], 1)
            self.assertEqual(metrics["emitted_rows"], 0)
            self.assertEqual(len(pd.read_parquet(processor.root / "batch-00000000000000000001" /
                                                "quarantine.parquet")), 6)

    def test_fail_policy_does_not_mutate_and_replay_source_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            processor = StatefulCallStream(Path(directory), SOURCE, late_policy="fail")
            row = event("a", 100.)
            processor.process(pd.DataFrame([row]), 0)
            before = json.dumps(processor.store.state_dict(), sort_keys=True)
            with self.assertRaisesRegex(RuntimeError, "Late CALL"):
                processor.process(pd.DataFrame([event("old", 1.)]), 1)
            with self.assertRaisesRegex(ValueError, "different input"):
                processor.process(pd.DataFrame([event("changed", 101.)]), 0)
            self.assertEqual(before, json.dumps(processor.store.state_dict(), sort_keys=True))
            with self.assertRaisesRegex(ValueError, "different Kafka source"):
                StatefulCallStream(Path(directory), {**SOURCE, "topic": "other"}, late_policy="fail")

    def test_inclusive_window_boundaries(self):
        rows = [event(str(index), ts) for index, ts in enumerate((0., 60., 60.001, 3600., 3600.001))]
        with tempfile.TemporaryDirectory() as directory:
            values = []
            for batch_id, row in enumerate(rows):
                processor = StatefulCallStream(Path(directory), SOURCE)
                values.append(processor.process(pd.DataFrame([row]), batch_id))
            actual = pd.concat(values).set_index("event_id")[CALL_FEATURES].sort_index()
            pd.testing.assert_frame_equal(actual, reference(rows), atol=1e-9, rtol=1e-9)



    def test_order_independent_retry_buffer_capacity_and_legacy_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            processor = StatefulCallStream(path, SOURCE, allowed_lateness_s=10, max_buffered_events=2)
            before = json.dumps(processor.store.state_dict(), sort_keys=True)
            with self.assertRaisesRegex(RuntimeError, "buffer capacity"):
                processor.process(pd.DataFrame([event("a", 100.), event("b", 101.), event("c", 102.)]), 0)
            self.assertEqual(processor.last_batch, -1)
            self.assertEqual(before, json.dumps(processor.store.state_dict(), sort_keys=True))
            frame = pd.DataFrame([event("a", 100.), event("b", 101.)])
            output = processor.process(frame, 0)
            pd.testing.assert_frame_equal(output, processor.process(frame.iloc[::-1], 0))
            self.assertEqual(processor.last_metrics["buffered_rows"], 2)
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "call_features_v1").mkdir()
            with self.assertRaisesRegex(ValueError, "Legacy v1 checkpoint"):
                StatefulCallStream(Path(directory), SOURCE)



    def test_optional_metadata_ttl_and_finite_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for value in (float("nan"), float("inf")):
                with self.assertRaisesRegex(ValueError, "finite"):
                    StatefulCallStream(path, SOURCE, allowed_lateness_s=value)
            processor = StatefulCallStream(path, SOURCE, metadata_ttl_s=86400.)
            processor.process(pd.DataFrame([event("a", 0.)]), 0)
            processor.process(pd.DataFrame([event("b", 100000., caller="C", callee="D")]), 1)
            self.assertNotIn("A", processor.store.first_seen)
            result = processor.process(pd.DataFrame([event("c", 100001.)]), 2)
            self.assertEqual(result.iloc[0].active_hours_on_network, 0.)


class ProducerTests(unittest.TestCase):
    def test_speed_waits_before_send(self):
        now, sent = [0.], []
        class Future:
            def get(self, timeout):
                return None
        class Producer:
            def send(self, topic, **kwargs):
                sent.append(now[0])
                return Future()
        replay([event("a", 0.), event("b", 10.), event("c", 30.)], Producer(), "test",
               speed=2., clock=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay))
        self.assertEqual(sent, [0., 5., 15.])


if __name__ == "__main__":
    unittest.main()

class IntegerCapacityValidationTests(unittest.TestCase):
    def test_non_integer_capacities_rejected_before_checkpoint_creation(self):
        invalid_values = (1.5, 2.0, True)
        settings = (
            {"retain_snapshots": 1},
            {"max_buffered_events": 1},
        )
        for setting in settings:
            for invalid in invalid_values:
                with self.subTest(setting=setting, invalid=invalid), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    kwargs = {**setting, next(iter(setting)): invalid}
                    with self.assertRaises(ValueError):
                        StatefulCallStream(root, SOURCE, **kwargs)
                    self.assertFalse((root / "call_features_v2").exists())


if __name__ == "__main__":
    unittest.main()
