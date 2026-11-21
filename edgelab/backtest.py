"""Vectorized next-bar-open backtester and performance statistics.

Timing (no lookahead):
  s[t]   target position decided with data up to the close of bar t
  held   from open[t+1] to open[t+2]  -> earns s[t] * (open[t+2]/open[t+1] - 1)
  cost   charged on |s[t] - s[t-1]| at open[t+1] (fee + slippage, per unit of turnover)

Expressed on the bar grid: return of interval k (open[k] -> open[k+1]) = s[k-1] * oo[k].
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np
import pandas as pd

_N = NormalDist()


def strategy_returns(df: pd.DataFrame, pos: np.ndarray, cost: float,
                     start: int = 0, end: int | None = None) -> tuple[np.ndarray, int, float]:
    """Per-bar net returns over bars [start, end), number of entries, and exposure.

    `cost` is the one-way cost as a fraction (0.001 = 10 bps) per unit of position change.
    """
    o = df["open"].to_numpy()
    oo = np.empty_like(o)
    oo[:-1] = o[1:] / o[:-1] - 1.0
    oo[-1] = 0.0  # last bar's forward interval is unknown
    held = np.concatenate([[0.0], pos[:-1]])  # s[k-1] held over interval k
    prev = np.concatenate([[0.0], held[:-1]])
    end = len(df) if end is None else end
    # Flatten at window start so a position carried from warm-up is entered (and paid for) here.
    h = held[start:end].copy()
    p = prev[start:end].copy()
    p[0] = 0.0
    net = h * oo[start:end] - cost * np.abs(h - p)
    entries = int(np.sum((h != 0) & (h != p)))
    exposure = float(np.mean(h != 0)) if len(h) else 0.0
    return net, entries, exposure


@dataclass
class Stats:
    sharpe: float          # annualized
    sr_bar: float          # per-bar Sharpe (for DSR)
    total_return: float
    cagr: float
    max_dd: float
    trades: int
    exposure: float
    skew: float
    kurt: float            # raw kurtosis (normal = 3)
    n: int
    win_rate: float        # fraction of in-market bars that were positive

    def as_dict(self, prefix: str = "") -> dict:
        return {prefix + k: v for k, v in self.__dict__.items()}


def stats(r: np.ndarray, trades: int, exposure: float, bpy: float) -> Stats:
    n = len(r)
    if n < 2:
        return Stats(0, 0, 0, 0, 0, trades, exposure, 0, 3, n, 0)
    mu, sd = float(np.mean(r)), float(np.std(r, ddof=1))
    sr = mu / sd if sd > 0 else 0.0
    eq = np.cumprod(1 + r)
    total = float(eq[-1] - 1)
    years = n / bpy
    cagr = float(eq[-1] ** (1 / years) - 1) if eq[-1] > 0 and years > 0 else -1.0
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))
    dd = float(np.min(np.concatenate([[1.0], eq]) / peak - 1))
    if sd > 0:
        zc = (r - mu) / sd
        skew = float(np.mean(zc ** 3))
        kurt = float(np.mean(zc ** 4))
    else:
        skew, kurt = 0.0, 3.0
    active = r[r != 0]
    win = float(np.mean(active > 0)) if len(active) else 0.0
    return Stats(sr * math.sqrt(bpy), sr, total, cagr, dd, trades, exposure, skew, kurt, n, win)


def deflated_sharpe(sr_bar: float, n_obs: int, skew: float, kurt: float,
                    n_trials: int, var_trials_sr: float | None = None) -> float:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014).

    Probability that the true Sharpe exceeds the best Sharpe you'd expect from `n_trials`
    skill-less strategies. `var_trials_sr` is the variance of per-bar Sharpe across those
    skill-less trials; by default it is the null sampling variance 1/(T-1). (The paper's
    alternative, the empirical variance across your trials, is inflated by real effects: every
    real edge has a mirrored losing version in the grid, which makes the bar unreachable.)
    Treating every trial as independent overstates N, since many candidates are near-duplicates,
    which keeps this conservative.
    """
    if n_obs < 3 or n_trials < 1:
        return 0.0
    if var_trials_sr is None:
        var_trials_sr = 1.0 / (n_obs - 1)
    g = 0.5772156649  # Euler-Mascheroni
    if n_trials > 1 and var_trials_sr > 0:
        sr0 = math.sqrt(var_trials_sr) * ((1 - g) * _N.inv_cdf(1 - 1 / n_trials)
                                          + g * _N.inv_cdf(1 - 1 / (n_trials * math.e)))
    else:
        sr0 = 0.0
    denom = 1 - skew * sr_bar + (kurt - 1) / 4 * sr_bar ** 2
    if denom <= 0:
        return 0.0
    z = (sr_bar - sr0) * math.sqrt(n_obs - 1) / math.sqrt(denom)
    return _N.cdf(z)


def benjamini_hochberg(pvals: list[float], q: float) -> list[bool]:
    """FDR control across all strategies sent to the out-of-sample gauntlet."""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    cutoff = -1
    for rank, i in enumerate(order, 1):
        if pvals[i] <= rank / m * q:
            cutoff = rank
    passed = [False] * m
    for rank, i in enumerate(order, 1):
        passed[i] = rank <= cutoff
    return passed
