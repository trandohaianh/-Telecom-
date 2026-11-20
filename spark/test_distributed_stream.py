"""Persistent worker-state kernels; no Spark installation needed."""
import json
import unittest

import pandas as pd
from distributed_stream import entity_partition, event_records, state_records
from stateful_stream import CALL_FEATURES
from test_stateful_stream import event, reference


class Record(dict):
    def asDict(self, recursive=True):
        return dict(self)


def stage(rows, previous, entity, watermark):
    records = [state_records(Record(key=key, payload=payload)) for key, payload in previous.items()]
    records += [item for row in rows for item in event_records(Record(row), entity)]
    output = list(entity_partition(iter(sorted(records, key=lambda item: item[0])),
                                   entity=entity, watermark=watermark, metadata_ttl_s=None))
    features = {event_id: json.loads(payload) for _, kind, event_id, payload in output if kind == "feature"}
    state = {key: payload for key, kind, _, payload in output if kind == "state"}
    return features, state


class PersistentKernelTests(unittest.TestCase):
    def test_cross_key_state_recovery_and_first_seen(self):
        from data_generator import REGISTERED_CELLS
        cell = next(iter(REGISTERED_CELLS))
        rows = [event("a", 0., duration_s=100., cell_id=cell),
                event("b", 50., duration_s=3., cell_id=cell),
                event("c", 60., caller="B", callee="A", cell_id=cell),
                event("d", 60., caller="A", callee="C", cell_id=cell),
                event("e", 60., caller="A", callee="A", imei=""),
                event("f", 3600.001, caller="C", callee="B", cell_id=cell),
                event("g", 86459., caller="A", callee="B", cell_id=cell)]
        states, combined = {"subscriber": {}, "imei": {}, "cell": {}}, {}
        for batch in (rows[:3], rows[3:6], rows[6:]):
            watermark = max(row["ts"] for row in batch)
            core, states["subscriber"] = stage(batch, states["subscriber"], "subscriber", watermark)
            imei, states["imei"] = stage(batch, states["imei"], "imei", watermark)
            enriched = [{**row, "active_hours_on_network": core[row["event_id"]]["active_hours_on_network"]}
                        for row in batch]
            cells, states["cell"] = stage(enriched, states["cell"], "cell", watermark)
            combined.update({event_id: {**values, **imei[event_id], **cells[event_id]}
                             for event_id, values in core.items()})
            # Durable state is serialized JSON, not a retained Python object.
            states = json.loads(json.dumps(states))
        actual = pd.DataFrame.from_dict(combined, orient="index")[CALL_FEATURES].astype(float).sort_index()
        actual.index.name = "event_id"
        pd.testing.assert_frame_equal(actual, reference(rows), atol=1e-9, rtol=1e-9)
        self.assertEqual(json.loads(states["subscriber"]["A"])["first_seen"]["A"], 0.)




class PersistentContractTests(unittest.TestCase):
    def test_manifest_namespace_and_configuration_binding(self):
        import tempfile
        from pathlib import Path

        from distributed_stream import DistributedCallStream
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory)
            source = {"bootstrap": "test", "topic": "isolated"}
            processor = DistributedCallStream(None, checkpoint, source, partitions=4)
            self.assertEqual(processor.root.name, "distributed_call_features_v1")
            with self.assertRaisesRegex(ValueError, "configuration changed"):
                DistributedCallStream(None, checkpoint, source, partitions=8)
            with self.assertRaisesRegex(ValueError, "configuration changed"):
                DistributedCallStream(None, checkpoint, {**source, "topic": "other"}, partitions=4)
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory)
            (checkpoint / "call_features_v2").mkdir()
            with self.assertRaisesRegex(ValueError, "fresh checkpoint"):
                DistributedCallStream(None, checkpoint, source)
        for value in (float("nan"), float("inf")):
            with self.assertRaisesRegex(ValueError, "finite"):
                DistributedCallStream(None, ".", source, allowed_lateness_s=value)

    def test_idle_key_state_expires_windows_but_preserves_first_seen(self):
        row = event("a", 0.)
        _, subscriber = stage([row], {}, "subscriber", 0.)
        _, later = stage([], subscriber, "subscriber", 100000.)
        saved = json.loads(later["A"])
        self.assertEqual(saved["first_seen"], {"A": 0.})
        self.assertEqual(saved["out24"], {})
        state = [state_records(Record(key=key, payload=payload)) for key, payload in subscriber.items()]
        expired = list(entity_partition(iter(sorted(state, key=lambda item: item[0])),
            entity="subscriber", watermark=100000., metadata_ttl_s=86400.))
        self.assertEqual(expired, [])
        for entity, horizon in (("imei", 86400.), ("cell", 3600.)):
            original = {"key": json.dumps({"A": 0.})}
            _, on_boundary = stage([], original, entity, horizon)
            self.assertIn("key", on_boundary)
            _, expired = stage([], original, entity, horizon + .001)
            self.assertEqual(expired, {})


if __name__ == "__main__":
    unittest.main()

class DistributedIntegerCapacityValidationTests(unittest.TestCase):
    def test_non_integer_capacities_rejected_before_checkpoint_creation(self):
        import tempfile
        from pathlib import Path

        from distributed_stream import DistributedCallStream

        invalid_settings = (
            ("partitions", 8),
            ("max_buffered_events", 100),
            ("retain_snapshots", 3),
        )
        source = {"bootstrap": "test", "topic": "integer-validation"}
        for name, _valid in invalid_settings:
            for invalid in (1.5, 2.0, True):
                with self.subTest(name=name, invalid=invalid), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    with self.assertRaises(ValueError):
                        DistributedCallStream(None, root, source, **{name: invalid})
                    self.assertFalse((root / DistributedCallStream.NAMESPACE).exists())


if __name__ == "__main__":
    unittest.main()
