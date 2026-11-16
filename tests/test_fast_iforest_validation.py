"""Native Isolation Forest inputs and degenerate training sets."""
from pathlib import Path
import sys
import numpy as np
import pytest
from sklearn.ensemble import IsolationForest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import fast_iforest
from fast_iforest import FastIForest


@pytest.mark.parametrize("native", [False, True])
def test_singleton_forest_matches_sklearn(native, monkeypatch):
    if not native:
        monkeypatch.setattr(fast_iforest, "_walk", None)
    model = IsolationForest(n_estimators=3, random_state=5).fit([[1., 2.]])
    query = np.array([[0., 0.], [1., 2.], [9., -1.]])
    assert np.allclose(FastIForest(model).score(query), -model.score_samples(query), atol=1e-12)


@pytest.mark.parametrize("query", [
    [[1.]], [[1., 2., 3.]], [1., 2.], [[np.nan, 1.]], [[np.inf, 2.]],
    [[10 ** 400, 1.]], [[1e308, 1.]], [[1+2j, 1.]], [["bad", 1.]],
])
def test_invalid_matrix_never_reaches_unchecked_native_walker(query, monkeypatch):
    model = FastIForest(IsolationForest(n_estimators=3, random_state=5).fit([[0., 1.], [2., 3.]]))
    def unexpected_native(*args):
        raise AssertionError("invalid input reached native code")
    monkeypatch.setattr(fast_iforest, "_walk", unexpected_native)
    with pytest.raises(ValueError):
        model.score(query)


def test_subsampled_feature_mapping_and_empty_matrix_match_sklearn():
    rng = np.random.default_rng(9)
    train, query = rng.normal(size=(50, 6)), rng.normal(size=(12, 6))
    model = IsolationForest(n_estimators=7, max_features=.5, random_state=8).fit(train)
    fast = FastIForest(model)
    assert np.allclose(fast.score(query), -model.score_samples(query), atol=1e-12)
    assert fast.score(np.empty((0, 6))).shape == (0,)


def test_threshold_boundaries_follow_sklearn_float32_input_contract():
    model = IsolationForest(n_estimators=5, max_samples=32, random_state=11).fit(
        np.random.default_rng(7).normal(size=(80, 3)))
    rows = []
    for estimator in model.estimators_:
        tree = estimator.tree_
        for node, feature in enumerate(tree.feature):
            if feature < 0:
                continue
            threshold = tree.threshold[node]
            for value in (np.nextafter(threshold, -np.inf), threshold,
                          np.nextafter(threshold, np.inf)):
                row = np.zeros(3)
                row[feature] = value
                rows.append(row)
    query = np.asarray(rows)
    assert np.allclose(FastIForest(model).score(query), -model.score_samples(query), atol=1e-12)
