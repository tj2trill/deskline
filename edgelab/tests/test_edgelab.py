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
