"""Market data: CSV loading, Binance public klines, and synthetic / permuted charts.

Every chart is a DataFrame indexed by UTC timestamp with float columns
open, high, low, close, volume (volume may be all-NaN if the source has none).
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

COLS = ["open", "high", "low", "close", "volume"]


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    date_col = next((c for c in ("date", "datetime", "timestamp", "time", "open_time") if c in df.columns), None)
    if date_col is None:
        raise ValueError(f"no date column among {list(df.columns)}")
    ts = df[date_col]
    if np.issubdtype(ts.dtype, np.number):  # epoch seconds or ms
        ts = pd.to_datetime(ts, unit="ms" if ts.iloc[0] > 1e11 else "s", utc=True)
    else:
        ts = pd.to_datetime(ts, utc=True)
    df.index = ts
    df.index.name = "time"
    missing = [c for c in ("open", "high", "low", "close") if c not in df.columns]
    if missing:
        raise ValueError(f"missing columns {missing}")
    if "volume" not in df.columns:
        df["volume"] = np.nan
    df = df[COLS].astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]
    return df


def load_csv(path: str | Path) -> pd.DataFrame:
    """Load OHLC(V) CSV. Accepts Yahoo-style (Date,Open,High,Low,Close,Adj Close,Volume) and similar."""
    return _normalize(pd.read_csv(path))


def load_dir(path: str | Path) -> dict[str, pd.DataFrame]:
    out = {}
    for p in sorted(Path(path).glob("*.csv")):
        out[p.stem] = load_csv(p)
    if not out:
        raise FileNotFoundError(f"no *.csv files in {path}")
    return out


def fetch_binance(symbol: str, interval: str = "1d", start: str = "2017-01-01",
                  base: str = "https://data-api.binance.vision") -> pd.DataFrame:
    """Public Binance spot klines (no API key). Paginates 1000 bars per request.

    data-api.binance.vision is Binance's public market-data mirror; api.binance.com works too
    but is geo-blocked in some regions.
    """
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    rows: list[list] = []
    while True:
        q = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "startTime": start_ms, "limit": 1000})
        with urllib.request.urlopen(f"{base}/api/v3/klines?{q}", timeout=30) as r:
            batch = json.loads(r.read())
        if not batch:
            break
        rows.extend(batch)
        start_ms = batch[-1][0] + 1
        if len(batch) < 1000:
            break
        time.sleep(0.2)  # stay well under Binance's public weight limit
    df = pd.DataFrame([r[:6] for r in rows], columns=["open_time", *COLS])
    df = _normalize(df)
    # Drop the still-forming last bar: its close is not final.
    if len(df) > 1:
        df = df.iloc[:-1]
    return df


# ---------------------------------------------------------------- synthetic charts

def random_walk(n: int, seed: int | None = None, vol: float = 0.02, drift: float = 0.0,
                start: str = "2015-01-01", freq: str = "D", p0: float = 100.0) -> pd.DataFrame:
    """Geometric random walk OHLCV. Contains no exploitable structure by construction."""
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, n)
    return _ohlc_from_returns(r, rng, vol, start, freq, p0)


def planted_edge(n: int, seed: int | None = None, vol: float = 0.02, phi: float = -0.25,
                 start: str = "2015-01-01", freq: str = "D", p0: float = 100.0) -> pd.DataFrame:
    """AR(1) returns: r_t = phi * r_{t-1} + e_t. phi<0 plants short-term mean reversion,
    phi>0 plants momentum. Used to check the pipeline can find a real edge."""
    rng = np.random.default_rng(seed)
    e = rng.normal(0.0, vol, n)
    r = np.empty(n)
    r[0] = e[0]
    for i in range(1, n):
        r[i] = phi * r[i - 1] + e[i]
    return _ohlc_from_returns(r, rng, vol, start, freq, p0)


def _ohlc_from_returns(r, rng, vol, start, freq, p0) -> pd.DataFrame:
    n = len(r)
    close = p0 * np.exp(np.cumsum(r))
    prev = np.concatenate([[p0], close[:-1]])
    # Open sits between prev close and close; wicks extend beyond the body.
    open_ = prev * np.exp(rng.normal(0, vol * 0.15, n))
    body_hi = np.maximum(open_, close)
    body_lo = np.minimum(open_, close)
    high = body_hi * np.exp(np.abs(rng.normal(0, vol * 0.5, n)))
    low = body_lo * np.exp(-np.abs(rng.normal(0, vol * 0.5, n)))
    volume = rng.lognormal(10, 0.5, n)
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


def permute_bars(df: pd.DataFrame, rng: np.random.Generator, keep_first: int = 0) -> pd.DataFrame:
    """A 'random chart' built from the real chart's own bars, shuffled in time.

    Each bar is decomposed into log-relatives (gap = open/prev_close, and high/low/close vs open);
    the gaps and the intrabar shapes are permuted independently and the chart is rebuilt.
    This keeps the bar-return distribution (volatility, fat tails, total drift) but destroys
    every serial dependency — which is exactly what a technical/timing edge would exploit.
    The first `keep_first` bars are left untouched (used as indicator warm-up).
    """
    lo, hi, cl, op = (np.log(df[c].to_numpy()) for c in ("low", "high", "close", "open"))
    n = len(df)
    k = max(int(keep_first), 1)
    if n - k < 2:
        return df.copy()
    gap = op[k:] - cl[k - 1:-1]
    rh, rl, rc = hi[k:] - op[k:], lo[k:] - op[k:], cl[k:] - op[k:]
    pg = rng.permutation(n - k)
    pb = rng.permutation(n - k)
    gap, rh, rl, rc = gap[pg], rh[pb], rl[pb], rc[pb]
    new_cl = cl[k - 1] + np.cumsum(gap + rc)  # each close = prev close + gap + body
    new_op = new_cl - rc
    out = df.copy()
    out.iloc[k:, out.columns.get_loc("open")] = np.exp(new_op)
    out.iloc[k:, out.columns.get_loc("close")] = np.exp(new_cl)
    out.iloc[k:, out.columns.get_loc("high")] = np.exp(new_op + rh)
    out.iloc[k:, out.columns.get_loc("low")] = np.exp(new_op + rl)
    if out["volume"].notna().any():
        vol = out["volume"].to_numpy().copy()
        vol[k:] = vol[k:][pb]  # volume travels with its bar shape
        out["volume"] = vol
    return out


def bars_per_year(df: pd.DataFrame) -> float:
    """Annualization factor = bars actually observed per calendar year. This handles 24/7 vs
    weekday-only markets and any session length without guessing. Pass --bars-per-year to override
    (e.g. if your file has large gaps)."""
    if len(df) < 3:
        return 252.0
    years = (df.index[-1] - df.index[0]).total_seconds() / (365.25 * 86400)
    return (len(df) - 1) / years if years > 0 else 252.0
