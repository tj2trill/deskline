"""The edge library: signal families x parameter grids x direction x regime filter.

Every family maps a chart to a target position in {-1, 0, +1} at each bar's close, using only
data up to and including that bar. The backtester shifts it forward one bar, so a value
decided at close t is first held from the open of t+1.

To add an edge: write `def fam_xxx(df, ...) -> np.ndarray` and register it in FAMILIES with a
parameter grid. It is picked up by the generator automatically.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- indicator helpers


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(dn != 0, 100.0).where(up.notna())


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def _state(enter_long, enter_short, exit_=None, hold: int | None = None) -> np.ndarray:
    """Event-driven position: +1 on enter_long, -1 on enter_short, 0 on exit, else keep.
    `hold` caps how many bars a position lasts after its entry event."""
    ev = pd.Series(np.nan, index=enter_long.index)
    if exit_ is not None:
        ev[exit_.fillna(False).astype(bool)] = 0.0
    ev[enter_short.fillna(False).astype(bool)] = -1.0
    ev[enter_long.fillna(False).astype(bool)] = 1.0
    if hold is None:
        pos = ev.ffill()
    elif hold == 1:
        pos = ev  # position lives only on the event bar
    else:
        pos = ev.ffill(limit=hold - 1)
    return pos.fillna(0.0).to_numpy()


def _sign(x: pd.Series) -> np.ndarray:
    return np.nan_to_num(np.sign(x.to_numpy()), nan=0.0)


# ---------------------------------------------------------------- signal families

def fam_sma_cross(df, fast, slow):
    c = df["close"]
    return _sign(sma(c, fast) - sma(c, slow))


def fam_ema_cross(df, fast, slow):
    c = df["close"]
    return _sign(ema(c, fast) - ema(c, slow))


def fam_price_vs_sma(df, n):
    c = df["close"]
    return _sign(c - sma(c, n))


def fam_ma_slope(df, n, k):
    m = sma(df["close"], n)
    return _sign(m - m.shift(k))


def fam_tsmom(df, lookback):
    c = df["close"]
    return _sign(c / c.shift(lookback) - 1)


def fam_reversal(df, lookback, hold):
    c = df["close"]
    r = c / c.shift(lookback) - 1
    return _state(r < 0, r > 0, hold=hold)


def fam_donchian(df, n, hold):
    c = df["close"]
    hi = df["high"].rolling(n, min_periods=n).max().shift(1)
    lo = df["low"].rolling(n, min_periods=n).min().shift(1)
    return _state(c > hi, c < lo, hold=hold)


def fam_bb_revert(df, n, k):
    c = df["close"]
    m, sd = sma(c, n), c.rolling(n, min_periods=n).std()
    cross_mid = np.sign(c - m) != np.sign(c.shift(1) - m.shift(1))
    return _state(c < m - k * sd, c > m + k * sd, exit_=cross_mid)


def fam_bb_breakout(df, n, k):
    c = df["close"]
    m, sd = sma(c, n), c.rolling(n, min_periods=n).std()
    cross_mid = np.sign(c - m) != np.sign(c.shift(1) - m.shift(1))
    return _state(c > m + k * sd, c < m - k * sd, exit_=cross_mid)


def fam_rsi_revert(df, n, lo):
    r = rsi(df["close"], n)
    cross50 = np.sign(r - 50) != np.sign(r.shift(1) - 50)
    return _state(r < lo, r > 100 - lo, exit_=cross50)


def fam_rsi_momentum(df, n, th):
    r = rsi(df["close"], n)
    return _state(r > th, r < 100 - th, exit_=(r - 50).abs() < 2)


def fam_macd(df, fast, slow, sig):
    c = df["close"]
    line = ema(c, fast) - ema(c, slow)
    return _sign(line - ema(line, sig))


def fam_zscore_revert(df, n, z):
    c = df["close"]
    zs = (c - sma(c, n)) / c.rolling(n, min_periods=n).std()
    cross0 = np.sign(zs) != np.sign(zs.shift(1))
    return _state(zs < -z, zs > z, exit_=cross0)


def fam_stoch_revert(df, n, lo):
    hh = df["high"].rolling(n, min_periods=n).max()
    ll = df["low"].rolling(n, min_periods=n).min()
    k = 100 * (df["close"] - ll) / (hh - ll).replace(0, np.nan)
    cross50 = np.sign(k - 50) != np.sign(k.shift(1) - 50)
    return _state(k < lo, k > 100 - lo, exit_=cross50)


def fam_keltner_breakout(df, n, m):
    c = df["close"]
    mid, a = ema(c, n), atr(df, n)
    cross_mid = np.sign(c - mid) != np.sign(c.shift(1) - mid.shift(1))
    return _state(c > mid + m * a, c < mid - m * a, exit_=cross_mid)


def fam_streak(df, k, hold, mode):
    d = np.sign(df["close"].diff())
    up = d.rolling(k, min_periods=k).sum() == k
    dn = d.rolling(k, min_periods=k).sum() == -k
    if mode == "revert":
        return _state(dn, up, hold=hold)
    return _state(up, dn, hold=hold)


def fam_near_high(df, n, pct):
    c = df["close"]
    hh, ll = c.rolling(n, min_periods=n).max(), c.rolling(n, min_periods=n).min()
    return _state(c >= hh * (1 - pct), c <= ll * (1 + pct), exit_=(c < hh * (1 - 2 * pct)) & (c > ll * (1 + 2 * pct)))


def fam_volume_spike(df, n, mult, hold):
    v = df["volume"]
    if v.isna().all():
        return np.zeros(len(df))
    spike = v > mult * sma(v, n)
    up = df["close"] > df["open"]
    return _state(spike & up, spike & ~up, hold=hold)


def fam_day_of_week(df, day):
    # Position decided at close t is held during bar t+1, so key on the *next* bar's weekday.
    # The calendar is known in advance, so this is not lookahead.
    nxt = pd.Series(df.index.dayofweek, index=df.index).shift(-1)
    return (nxt == day).astype(float).to_numpy()


def fam_vol_squeeze(df, n, hold):
    # Bollinger bandwidth at an n-bar low, then trade the direction of the breakout bar.
    c = df["close"]
    bw = c.rolling(20, min_periods=20).std() / sma(c, 20)
    squeeze = (bw <= bw.rolling(n, min_periods=n).min() * 1.05).shift(1)
    m = sma(c, 20)
    return _state(squeeze.fillna(False).astype(bool) & (c > m), squeeze.fillna(False).astype(bool) & (c < m), hold=hold)


def fam_inside_bar(df, hold):
    h, l = df["high"], df["low"]
    inside = (h.shift(1) < h.shift(2)) & (l.shift(1) > l.shift(2))
    return _state(inside & (df["close"] > h.shift(1)), inside & (df["close"] < l.shift(1)), hold=hold)


def fam_dual_momentum(df, short, long):
    c = df["close"]
    return _sign((c / c.shift(short) - 1) - (c / c.shift(long) - 1) * short / long)


FAMILIES: dict[str, tuple] = {
    "sma_cross": (fam_sma_cross, {"fast": [5, 10, 20, 50], "slow": [20, 50, 100, 200]}),
    "ema_cross": (fam_ema_cross, {"fast": [5, 12, 20, 50], "slow": [26, 50, 100, 200]}),
    "price_vs_sma": (fam_price_vs_sma, {"n": [10, 20, 50, 100, 200]}),
    "ma_slope": (fam_ma_slope, {"n": [20, 50, 100], "k": [5, 20]}),
    "tsmom": (fam_tsmom, {"lookback": [5, 10, 20, 60, 120, 250]}),
    "reversal": (fam_reversal, {"lookback": [1, 2, 3, 5, 10], "hold": [1, 3, 5]}),
    "donchian": (fam_donchian, {"n": [10, 20, 55, 100], "hold": [5, 20, None]}),
    "bb_revert": (fam_bb_revert, {"n": [10, 20, 50], "k": [1.5, 2.0, 2.5]}),
    "bb_breakout": (fam_bb_breakout, {"n": [10, 20, 50], "k": [1.0, 1.5, 2.0]}),
    "rsi_revert": (fam_rsi_revert, {"n": [2, 3, 5, 14], "lo": [10, 20, 30]}),
    "rsi_momentum": (fam_rsi_momentum, {"n": [7, 14, 21], "th": [55, 60, 70]}),
    "macd": (fam_macd, {"fast": [5, 8, 12], "slow": [17, 26, 35], "sig": [5, 9]}),
    "zscore_revert": (fam_zscore_revert, {"n": [5, 10, 20, 50], "z": [1.0, 1.5, 2.0]}),
    "stoch_revert": (fam_stoch_revert, {"n": [5, 14], "lo": [10, 20]}),
    "keltner_breakout": (fam_keltner_breakout, {"n": [10, 20, 50], "m": [1.0, 1.5, 2.0, 3.0]}),
    "streak": (fam_streak, {"k": [2, 3, 4, 5], "hold": [1, 3, 5], "mode": ["revert", "follow"]}),
    "near_high": (fam_near_high, {"n": [50, 100, 250], "pct": [0.02, 0.05]}),
    "volume_spike": (fam_volume_spike, {"n": [20, 50], "mult": [1.5, 2.0, 3.0], "hold": [1, 3, 5]}),
    "day_of_week": (fam_day_of_week, {"day": [0, 1, 2, 3, 4, 5, 6]}),
    "vol_squeeze": (fam_vol_squeeze, {"n": [60, 120], "hold": [5, 10, 20]}),
    "inside_bar": (fam_inside_bar, {"hold": [1, 3, 5, 10]}),
    "dual_momentum": (fam_dual_momentum, {"short": [10, 20], "long": [60, 120, 250]}),
}

DIRECTIONS = ("long", "short", "both")
FILTERS = ("none", "trend", "lowvol", "highvol")


def _filter_mask(df: pd.DataFrame, kind: str, pos: np.ndarray) -> np.ndarray:
    """Regime filter: zeroes positions outside the regime. Uses data up to bar t only."""
    if kind == "none":
        return pos
    if kind == "trend":  # longs only above the 200-bar SMA, shorts only below
        m = sma(df["close"], 200)
        above = (df["close"] > m).to_numpy()
        valid = m.notna().to_numpy()
        keep = valid & (((pos > 0) & above) | ((pos < 0) & ~above))
        return np.where(keep, pos, 0.0)
    a = (atr(df, 20) / df["close"])
    med = a.rolling(250, min_periods=100).median()
    valid = (a.notna() & med.notna()).to_numpy()
    low = (a < med).to_numpy()
    keep = valid & (low if kind == "lowvol" else ~low)
    return np.where(keep, pos, 0.0)


@dataclass(frozen=True)
class Strategy:
    family: str
    params: tuple  # sorted (name, value) pairs, hashable
    direction: str = "both"
    filter: str = "none"
    meta: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def id(self) -> str:
        p = ",".join(f"{k}={v}" for k, v in self.params)
        return f"{self.family}({p})|{self.direction}|{self.filter}"

    def positions(self, df: pd.DataFrame) -> np.ndarray:
        fn, _ = FAMILIES[self.family]
        pos = np.asarray(fn(df, **dict(self.params)), dtype=float)
        if self.direction == "long":
            pos = np.clip(pos, 0, 1)
        elif self.direction == "short":
            pos = np.clip(pos, -1, 0)
        return _filter_mask(df, self.filter, pos)

    @staticmethod
    def from_id(sid: str) -> "Strategy":
        head, direction, filt = sid.split("|")
        fam, rest = head.split("(", 1)
        params = []
        for kv in filter(None, rest.rstrip(")").split(",")):
            k, v = kv.split("=")
            params.append((k, _parse(v)))
        return Strategy(fam, tuple(params), direction, filt)


def _parse(v: str):
    if v == "None":
        return None
    for t in (int, float):
        try:
            return t(v)
        except ValueError:
            pass
    return v


def _valid(family: str, p: dict) -> bool:
    if "fast" in p and "slow" in p and p["fast"] >= p["slow"]:
        return False
    if family == "dual_momentum" and p["short"] >= p["long"]:
        return False
    return True


def generate(families: list[str] | None = None, directions=DIRECTIONS, filters=FILTERS) -> list[Strategy]:
    """Every combination of family x parameter grid x direction x regime filter."""
    out = []
    for fam in families or FAMILIES:
        _, grid = FAMILIES[fam]
        keys = sorted(grid)
        for vals in itertools.product(*(grid[k] for k in keys)):
            p = dict(zip(keys, vals))
            if not _valid(fam, p):
                continue
            for d in directions:
                for f in filters:
                    out.append(Strategy(fam, tuple(sorted(p.items())), d, f))
    return out
