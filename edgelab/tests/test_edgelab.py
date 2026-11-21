import numpy as np
import pandas as pd
import pytest

from edgelab import backtest as bt
from edgelab import data
from edgelab.pipeline import Config, run
from edgelab.signals import Strategy, generate

SILENT = lambda *a, **k: None  # noqa: E731


def test_no_lookahead_every_strategy():
    """A position at bar t must not change when bars after t are removed."""
    df = data.random_walk(700, seed=3)
    cuts = [450, 600]
    for s in generate():
        full = s.positions(df)
        for k in cuts:
            part = s.positions(df.iloc[:k])
            # day_of_week reads the *next* bar's calendar date, which a truncated frame lacks
            upto = k - 1 if s.family == "day_of_week" else k
            np.testing.assert_array_equal(full[:upto], part[:upto], err_msg=s.id)


def test_execution_is_next_open_and_costs_charged():
    idx = pd.date_range("2020-01-01", periods=5, freq="D", tz="UTC")
    o = np.array([100, 110, 121, 121, 100.0])
    df = pd.DataFrame({"open": o, "high": o, "low": o, "close": o, "volume": 1.0}, index=idx)
    pos = np.array([1, 1, 0, 0, 0.0])  # long decided at close of bar 0
    r, entries, _ = bt.strategy_returns(df, pos, cost=0.01)
    # bar 0: flat. bar1 interval (open1->open2): held s[0]=1 -> +10%, minus 1% entry cost
    # bar2: held s[1]=1 -> 121->121 = 0. bar3: held s[2]=0 -> exit cost 1%.
    np.testing.assert_allclose(r, [0.0, 0.10 - 0.01, 0.0, -0.01, 0.0])
    assert entries == 1


def test_permuted_chart_is_valid_and_keeps_distribution():
    df = data.planted_edge(600, seed=1)
    p = data.permute_bars(df, np.random.default_rng(0), keep_first=300)
    pd.testing.assert_frame_equal(p.iloc[:300], df.iloc[:300])
    assert (p["high"] >= p[["open", "close"]].max(axis=1) - 1e-9).all()
    assert (p["low"] <= p[["open", "close"]].min(axis=1) + 1e-9).all()
    # same bars in a different order -> same total move, same gap distribution
    assert np.isclose(p["close"].iloc[-1], df["close"].iloc[-1])
    g = lambda x: np.sort(np.log(x["open"].iloc[300:].to_numpy() / x["close"].iloc[299:-1].to_numpy()))  # noqa: E731
    np.testing.assert_allclose(g(p), g(df))
    assert not np.allclose(p["close"].to_numpy(), df["close"].to_numpy())


def test_deflated_sharpe_penalizes_more_trials():
    a = bt.deflated_sharpe(0.08, 1500, 0, 3, n_trials=1)
    b = bt.deflated_sharpe(0.08, 1500, 0, 3, n_trials=3000)
    assert a > 0.99 and b < a


def test_strategy_id_roundtrip():
    for s in generate()[::37]:
        assert Strategy.from_id(s.id) == s


def test_benjamini_hochberg():
    assert bt.benjamini_hochberg([0.001, 0.02, 0.04, 0.5], 0.1) == [True, True, True, False]


def _fast_cfg(**kw):
    base = dict(charts=20, perms=50, workers=4, seed=11)
    base.update(kw)
    return Config(**base)


def test_noise_universe_yields_no_keepers_even_when_stage1_is_wide_open():
    """Pure random walks: force the luckiest in-sample strategies into the gauntlet;
    the out-of-sample + shuffled-chart test must throw every one of them out."""
    u = {f"RW{i}": data.random_walk(2000, seed=500 + i, drift=0.0003) for i in range(5)}
    cfg = _fast_cfg(min_dsr=0.0, min_is_sharpe=-99, min_is_frac_positive=0.0, max_forward=20)
    res = run(u, cfg, log=SILENT)
    assert res["n_forwarded"] == 20
    assert res["keepers"] == []


def test_planted_edge_is_found_and_only_the_right_kind():
    u = {f"MR{i}": data.planted_edge(2000, seed=900 + i, phi=-0.2) for i in range(5)}
    cfg = _fast_cfg(families=["reversal", "rsi_revert", "tsmom", "sma_cross", "donchian"])
    res = run(u, cfg, log=SILENT)
    fams = {k["id"].split("(")[0] for k in res["keepers"]}
    assert fams, "a real planted edge should survive"
    assert fams <= {"reversal", "rsi_revert"}, fams


def test_batched_engine_matches_reference_for_every_strategy():
    from edgelab import batched as bx
    segs = [(data.random_walk(520, seed=40), 300), (data.planted_edge(600, seed=41), 300),
            (data.random_walk(560, seed=42, vol=0.04), 250)]
    hb = bx.HostBatch(segs, n_charts=3)  # different lengths -> exercises right-padding
    ops = bx.Ops("cpu")
    d = hb.to_device(ops, slice(0, hb.B))
    for s in generate():
        pb = bx.positions(ops, d, s)
        for j, (df, _) in enumerate(segs):
            n = len(df) - 1  # last bar's position is never held inside the chart
            np.testing.assert_array_equal(pb[:n, j], s.positions(df)[:n], err_msg=s.id)


@pytest.mark.parametrize("mem_gb", [0.0, 0.002])  # 0.002 GB forces many streamed column chunks
def test_batched_stage2_matches_reference(mem_gb):
    from edgelab import pipeline as pl
    u = {f"MR{i}": data.planted_edge(1500, seed=70 + i, phi=-0.2) for i in range(4)}
    cfg = _fast_cfg(charts=6, perms=20, window=200, workers=2, mem_gb=mem_gb)
    cfg.bars_per_year = 365.0
    split = pl.make_split(u, cfg)
    charts = pl.sample_charts(u, split, cfg)
    ids = ["rsi_revert(lo=30,n=2)|both|none", "sma_cross(fast=10,slow=50)|long|trend",
           "keltner_breakout(m=1.5,n=20)|short|lowvol", "day_of_week(day=2)|both|highvol"]
    ref = pl.stage2(u, split, charts, Config(**{**cfg.__dict__, "device": "pandas"}), ids, log=SILENT)
    new = pl.stage2(u, split, charts, cfg, ids, log=SILENT)
    pd.testing.assert_frame_equal(ref.set_index("id"), new.set_index("id"), check_exact=False, rtol=1e-9)
