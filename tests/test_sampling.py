import numpy as np

from sdhex.sampling import Aggregator


def test_aggregator_matches_naive():
    rng = np.random.default_rng(3)
    cells = 40
    idx = rng.integers(-1, cells, (60, 70)).astype(np.int32)
    idx[:, :3] = -1
    vals = rng.normal(100, 30, idx.shape)
    valid = rng.random(idx.shape) > 0.2
    cats = rng.integers(0, 5, idx.shape)
    agg = Aggregator(idx, cells)

    mean, std, vmin, vmax = agg.masked_stats(vals, valid)
    dom, share = agg.dominant(cats)
    frac = agg.fraction(cats == 2)
    for h in range(cells):
        sel = idx == h
        v = vals[sel & valid]
        if v.size:
            assert np.isclose(mean[h], v.mean())
            assert np.isclose(std[h], v.std())
            assert np.isclose(vmin[h], v.min()) and np.isclose(vmax[h], v.max())
        counts = np.bincount(cats[sel], minlength=5)
        if sel.any():
            assert counts[dom[h]] == counts.max()
            assert np.isclose(share[h], counts.max() / sel.sum())
            assert np.isclose(frac[h], (cats[sel] == 2).mean())


def test_dominant_ignore():
    idx = np.zeros((1, 5), dtype=np.int32)
    cats = np.array([[0, 0, 0, 7, 7]])
    agg = Aggregator(idx, 2)
    dom, _ = agg.dominant(cats, ignore=0)
    assert dom[0] == 7 and dom[1] == -1
