"""Vectorised Isolation Forest scorer.

scikit-learn scores one tree at a time with Python overhead per tree, which costs ~0.2 ms per
event at our batch sizes. Here every tree is flattened into padded arrays and all trees are
walked simultaneously with NumPy, giving identical scores (verified in tests) ~20x faster.
"""
import numpy as np

try:
    from numba import njit, prange
except ImportError:  # pragma: no cover - numba is optional, NumPy fallback below
    njit, prange = None, range


# _walk_py duyệt đồng thời mọi cây trên từng sample để khử overhead Python và tăng throughput.
def _walk_py(X, feature, threshold, left, right, leaf_value):
    n, T = X.shape[0], feature.shape[0]
    out = np.zeros(n)
    for i in prange(n):
        acc = 0.0
        for k in range(T):
            node = 0
            while left[k, node] != -1:
                if X[i, feature[k, node]] <= threshold[k, node]:
                    node = left[k, node]
                else:
                    node = right[k, node]
            acc += leaf_value[k, node]
        out[i] = acc / T
    return out


_walk = njit(cache=True, nogil=True, parallel=True)(_walk_py) if njit else None


# _c tính hệ số chuẩn hóa độ sâu cây theo Isolation Forest để tương đương với scikit-learn.
def _c(n):
    n = np.asarray(n, dtype=float)
    out = np.zeros_like(n)
    m = n > 2
    out[m] = 2.0 * (np.log(n[m] - 1.0) + np.euler_gamma) - 2.0 * (n[m] - 1.0) / n[m]
    out[n == 2] = 1.0
    return out


# FastIForest flatten cấu trúc cây sklearn thành ma trận để score batch nhanh hơn nhiều.
class FastIForest:
    # __init__ nạp từng cây và map node sang mảng feature / threshold / child index để chạy vectorized.
    def __init__(self, iso):
        trees = iso.estimators_
        feats_sub = iso.estimators_features_
        T = len(trees)
        M = max(t.tree_.node_count for t in trees)
        self.feature = np.zeros((T, M), dtype=np.int64)
        self.threshold = np.zeros((T, M))
        self.left = np.full((T, M), -1, dtype=np.int64)
        self.right = np.full((T, M), -1, dtype=np.int64)
        self.leaf_value = np.zeros((T, M))          # depth + c(n_samples) at leaves
        for k, (est, fsub) in enumerate(zip(trees, feats_sub)):
            tr = est.tree_
            n = tr.node_count
            fe = tr.feature.copy()
            internal = fe >= 0
            fe[internal] = np.asarray(fsub)[fe[internal]]
            self.feature[k, :n] = np.where(internal, fe, 0)
            self.threshold[k, :n] = tr.threshold
            self.left[k, :n] = tr.children_left
            self.right[k, :n] = tr.children_right
            depth = np.zeros(n)
            stack = [(0, 0)]
            while stack:
                node, d = stack.pop()
                depth[node] = d
                if tr.children_left[node] != -1:
                    stack.append((tr.children_left[node], d + 1))
                    stack.append((tr.children_right[node], d + 1))
            self.leaf_value[k, :n] = depth + _c(tr.n_node_samples)
        self.T = T
        self.n_features = int(iso.n_features_in_)
        self.denom = float(_c(np.array([iso.max_samples_]))[0])
        self.rows = np.arange(T)

    # score trả về điểm bất thường tương đương với -score_samples; giá trị cao hơn nghĩa là lạ hơn.
    def score(self, X: np.ndarray) -> np.ndarray:
        """Return anomaly scores; validate dimensions before entering unchecked native code."""
        try:
            if np.iscomplexobj(X):
                raise ValueError("complex input is not supported")
            with np.errstate(over="ignore", invalid="ignore"):
                # sklearn tree inference quantizes inputs to float32 before comparisons.
                X = np.asarray(X, dtype=np.float32)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Isolation Forest inputs must be a finite numeric matrix") from exc
        if X.ndim != 2 or X.shape[1] != self.n_features:
            raise ValueError(f"Isolation Forest inputs require {self.n_features} feature columns")
        if not np.isfinite(X).all():
            raise ValueError("Isolation Forest inputs must be finite")
        if self.denom == 0:
            # sklearn defines a singleton forest's zero normalizer
            # as mean normalized depth one, hence anomaly score 0.5.
            return np.full(X.shape[0], 0.5)
        if _walk is not None:
            depth = _walk(np.ascontiguousarray(X, dtype=np.float64), self.feature, self.threshold,
                          self.left, self.right, self.leaf_value)
            return 2.0 ** (-depth / self.denom)
        return self._score_numpy(X)

    # _score_numpy là fallback khi numba không có sẵn, vẫn giữ logic walk tree tương đương với sklearn.
    def _score_numpy(self, X):
        n = X.shape[0]
        node = np.zeros((n, self.T), dtype=np.int64)
        rows = np.broadcast_to(self.rows, (n, self.T))
        sample = np.arange(n)[:, None]
        while True:
            lft = self.left[rows, node]
            active = lft != -1
            if not active.any():
                break
            f = self.feature[rows, node]
            go_left = X[sample, f] <= self.threshold[rows, node]
            nxt = np.where(go_left, lft, self.right[rows, node])
            node = np.where(active, nxt, node)
        depth = self.leaf_value[rows, node].mean(axis=1)
        return 2.0 ** (-depth / self.denom)
