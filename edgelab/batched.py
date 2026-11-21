"""Batched signal + backtest engine: one strategy evaluated on thousands of charts at once.

Arrays are time-major, shape (T, B): row t is bar t of every chart, column b is one chart.
The same code runs on NumPy (CPU) or CuPy (GPU); `xp` is whichever module is active.

Sequential recurrences (EWM, event-state forward fill) cannot be vectorized over time. On the
GPU they are one CUDA kernel launch with one thread per chart; the time-major layout makes
neighbouring threads read neighbouring addresses (coalesced). On the CPU they are a loop over
T with vector ops across all B charts.

`signals.py` (pandas) is the reference implementation; tests require this engine to produce
identical positions.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- backend selection

GPU_MIN_CELLS = 500_000  # below ~T*B of this, launch overhead makes the GPU no faster than the CPU


def gpu_available() -> bool:
    try:
        import cupy
        return cupy.cuda.runtime.getDeviceCount() > 0
    except Exception:  # cupy missing, no driver, or no device: all mean "no GPU"
        return False


def choose_device(device: str, cells: int, min_cells: int = GPU_MIN_CELLS) -> str:
    if device == "gpu":
        if not gpu_available():
            raise RuntimeError("--device gpu: CuPy cannot see a CUDA device (pip install cupy-cuda12x; check nvidia-smi)")
        return "gpu"
    if device == "auto":
        return "gpu" if cells >= min_cells and gpu_available() else "cpu"
    if device != "cpu":
        raise ValueError(f"unknown device {device!r} (auto, gpu, cpu, pandas)")
    return device


_EWM_SRC = r"""
extern "C" __global__ void ewm(const double* x, double* y, int T, int B, double a, int minp) {
    int b = blockDim.x * blockIdx.x + threadIdx.x;
    if (b >= B) return;
    double s = nan(""); int cnt = 0;
    for (int t = 0; t < T; ++t) {
        double v = x[(long)t * B + b];
        if (!isnan(v)) { ++cnt; s = isnan(s) ? v : (1.0 - a) * s + a * v; }
        y[(long)t * B + b] = (cnt >= minp) ? s : nan("");
    }
}
extern "C" __global__ void ffill_state(const double* ev, double* out, int T, int B, int limit) {
    int b = blockDim.x * blockIdx.x + threadIdx.x;
    if (b >= B) return;
    double last = nan(""); int age = 0;
    for (int t = 0; t < T; ++t) {
        double v = ev[(long)t * B + b];
        if (!isnan(v)) { last = v; age = 0; } else { ++age; }
        out[(long)t * B + b] = (!isnan(last) && (limit < 0 || age <= limit)) ? last : 0.0;
    }
}
"""


class Ops:
    """Indicator primitives on (T, B) arrays for one backend."""

    def __init__(self, device: str):
        self.device = device
        if device == "gpu":
            import cupy
            self.xp = cupy
            mod = cupy.RawModule(code=_EWM_SRC)
            self._k_ewm = mod.get_function("ewm")
            self._k_ffill = mod.get_function("ffill_state")
        else:
            self.xp = np

    # -- transfers
    def asarray(self, a):
        return self.xp.asarray(a)

    def to_host(self, a) -> np.ndarray:
        return a.get() if self.device == "gpu" else a

    # -- elementwise helpers
    def shift(self, x, k: int = 1, fill=np.nan):
        out = self.xp.full_like(x, fill)
        if k < x.shape[0]:
            out[k:] = x[:-k]
        return out

    def sign0(self, x):
        return self.xp.nan_to_num(self.xp.sign(x), nan=0.0)

    # -- rolling windows (pandas min_periods=n semantics: NaN unless n valid values)
    def _rsum(self, x, n):
        xp = self.xp
        valid = ~xp.isnan(x)
        z = xp.zeros((1, x.shape[1]), dtype=x.dtype)
        cs = xp.concatenate([z, xp.cumsum(xp.where(valid, x, 0.0), axis=0)])
        cc = xp.concatenate([z, xp.cumsum(valid, axis=0, dtype=x.dtype)])
        s = xp.full_like(x, np.nan)
        cnt = xp.zeros_like(x)
        s[n - 1:] = cs[n:] - cs[:-n]
        cnt[n - 1:] = cc[n:] - cc[:-n]
        return s, cnt

    def sma(self, x, n):
        s, cnt = self._rsum(x, n)
        return self.xp.where(cnt == n, s / n, np.nan)

    def rstd(self, x, n):
        xp = self.xp
        ref = xp.nanmean(x, axis=0, keepdims=True)  # de-mean first: avoids cancellation in E[x^2]-E[x]^2
        d = x - ref
        s, cnt = self._rsum(d, n)
        s2, _ = self._rsum(d * d, n)
        var = xp.maximum((s2 - s * s / n) / (n - 1), 0.0)
        return xp.where(cnt == n, xp.sqrt(var), np.nan)

    def _rext(self, x, n, f):
        # Sparse-table trick: max over n = f(M_k(t), M_k(t - (n - 2^k))) with 2^k <= n, O(log n) ops.
        m, w = x, 1
        while w * 2 <= n:
            m = f(m, self.shift(m, w))
            w *= 2
        return f(m, self.shift(m, n - w)) if n > w else m

    def rmax(self, x, n):
        return self._rext(x, n, self.xp.maximum)

    def rmin(self, x, n):
        return self._rext(x, n, self.xp.minimum)

    # -- recurrences
    def ewm(self, x, alpha: float, minp: int):
        xp = self.xp
        x = xp.ascontiguousarray(x, dtype=xp.float64)
        T, B = x.shape
        if self.device == "gpu":
            y = xp.empty_like(x)
            threads = 128
            self._k_ewm(((B + threads - 1) // threads,), (threads,),
                        (x, y, np.int32(T), np.int32(B), np.float64(alpha), np.int32(minp)))
            return y
        y = np.empty_like(x)
        s = np.full(B, np.nan)
        cnt = np.zeros(B)
        for t in range(T):
            v = x[t]
            ok = ~np.isnan(v)
            cnt += ok
            s = np.where(ok, np.where(np.isnan(s), v, (1 - alpha) * s + alpha * v), s)
            y[t] = np.where(cnt >= minp, s, np.nan)
        return y

    def ema(self, x, n):
        return self.ewm(x, 2.0 / (n + 1), n)

    def state(self, enter_long, enter_short, exit_=None, hold: int | None = None):
        xp = self.xp
        ev = xp.full(enter_long.shape, np.nan)
        if exit_ is not None:
            ev = xp.where(exit_, 0.0, ev)
        ev = xp.where(enter_short, -1.0, ev)
        ev = xp.where(enter_long, 1.0, ev)
        limit = -1 if hold is None else hold - 1
        T, B = ev.shape
        if self.device == "gpu":
            out = xp.empty_like(ev)
            threads = 128
            self._k_ffill(((B + threads - 1) // threads,), (threads,),
                          (xp.ascontiguousarray(ev), out, np.int32(T), np.int32(B), np.int32(limit)))
            return out
        out = np.empty_like(ev)
        last = np.full(B, np.nan)
        age = np.zeros(B)
        for t in range(T):
            v = ev[t]
            ok = ~np.isnan(v)
            last = np.where(ok, v, last)
            age = np.where(ok, 0, age + 1)
            out[t] = np.where(~np.isnan(last) & ((limit < 0) | (age <= limit)), last, 0.0)
        return out

    def rsi(self, c, n):
        xp = self.xp
        d = c - self.shift(c, 1)
        up = self.ewm(xp.clip(d, 0, None), 1.0 / n, n)
        dn = self.ewm(-xp.clip(d, None, 0), 1.0 / n, n)
        rs = up / xp.where(dn == 0, np.nan, dn)
        out = 100 - 100 / (1 + rs)
        out = xp.where(dn != 0, out, 100.0)
        return xp.where(xp.isnan(up), np.nan, out)

    def atr(self, h, l, c, n):
        xp = self.xp
        pc = self.shift(c, 1)
        tr = xp.fmax(xp.fmax(h - l, xp.abs(h - pc)), xp.abs(l - pc))  # fmax skips NaN like pandas max
        return self.ewm(tr, 1.0 / n, n)

    def crosses(self, a, b):
        # pandas reference: np.sign(a-b) != np.sign(a.shift(1)-b.shift(1)); NaN != NaN is True there too
        xp = self.xp
        b1 = self.shift(b, 1) if hasattr(b, "shape") else b  # scalar level, e.g. RSI 50
        return xp.sign(a - b) != xp.sign(self.shift(a, 1) - b1)


# ---------------------------------------------------------------- batched signal families
# Each mirrors fam_* in signals.py. Arguments: ops, batch, params.

def b_sma_cross(k, d, fast, slow):
    return k.sign0(k.sma(d.c, fast) - k.sma(d.c, slow))


def b_ema_cross(k, d, fast, slow):
    return k.sign0(k.ema(d.c, fast) - k.ema(d.c, slow))


def b_price_vs_sma(k, d, n):
    return k.sign0(d.c - k.sma(d.c, n))


def b_ma_slope(k, d, n, k_):
    m = k.sma(d.c, n)
    return k.sign0(m - k.shift(m, k_))


def b_tsmom(k, d, lookback):
    return k.sign0(d.c / k.shift(d.c, lookback) - 1)


def b_reversal(k, d, lookback, hold):
    r = d.c / k.shift(d.c, lookback) - 1
    return k.state(r < 0, r > 0, hold=hold)


def b_donchian(k, d, n, hold):
    hi = k.shift(k.rmax(d.h, n), 1)
    lo = k.shift(k.rmin(d.l, n), 1)
    return k.state(d.c > hi, d.c < lo, hold=hold)


def _bands(k, d, n):
    m = k.sma(d.c, n)
    return m, k.rstd(d.c, n), k.crosses(d.c, m)


def b_bb_revert(k, d, n, k_):
    m, sd, x = _bands(k, d, n)
    return k.state(d.c < m - k_ * sd, d.c > m + k_ * sd, exit_=x)


def b_bb_breakout(k, d, n, k_):
    m, sd, x = _bands(k, d, n)
    return k.state(d.c > m + k_ * sd, d.c < m - k_ * sd, exit_=x)


def b_rsi_revert(k, d, n, lo):
    r = k.rsi(d.c, n)
    return k.state(r < lo, r > 100 - lo, exit_=k.crosses(r, 50.0))


def b_rsi_momentum(k, d, n, th):
    r = k.rsi(d.c, n)
    return k.state(r > th, r < 100 - th, exit_=k.xp.abs(r - 50) < 2)


def b_macd(k, d, fast, slow, sig):
    line = k.ema(d.c, fast) - k.ema(d.c, slow)
    return k.sign0(line - k.ema(line, sig))


def b_zscore_revert(k, d, n, z):
    zs = (d.c - k.sma(d.c, n)) / k.rstd(d.c, n)
    return k.state(zs < -z, zs > z, exit_=k.crosses(zs, 0.0))


def b_stoch_revert(k, d, n, lo):
    xp = k.xp
    hh, ll = k.rmax(d.h, n), k.rmin(d.l, n)
    rng = hh - ll
    st = 100 * (d.c - ll) / xp.where(rng == 0, np.nan, rng)
    return k.state(st < lo, st > 100 - lo, exit_=k.crosses(st, 50.0))


def b_keltner_breakout(k, d, n, m):
    mid, a = k.ema(d.c, n), k.atr(d.h, d.l, d.c, n)
    return k.state(d.c > mid + m * a, d.c < mid - m * a, exit_=k.crosses(d.c, mid))


def b_streak(k, d, k_, hold, mode):
    s, cnt = k._rsum(k.xp.sign(d.c - k.shift(d.c, 1)), k_)
    s = k.xp.where(cnt == k_, s, np.nan)
    up, dn = s == k_, s == -k_
    return k.state(dn, up, hold=hold) if mode == "revert" else k.state(up, dn, hold=hold)


def b_near_high(k, d, n, pct):
    hh, ll = k.rmax(d.c, n), k.rmin(d.c, n)
    ex = (d.c < hh * (1 - 2 * pct)) & (d.c > ll * (1 + 2 * pct))
    return k.state(d.c >= hh * (1 - pct), d.c <= ll * (1 + pct), exit_=ex)


def b_volume_spike(k, d, n, mult, hold):
    spike = d.v > mult * k.sma(d.v, n)  # all-NaN volume -> never True -> flat, as in the reference
    up = d.c > d.o
    return k.state(spike & up, spike & ~up, hold=hold)


def b_day_of_week(k, d, day):
    return (d.dow_next == day).astype(k.xp.float64)


def b_vol_squeeze(k, d, n, hold):
    xp = k.xp
    m = k.sma(d.c, 20)
    bw = k.rstd(d.c, 20) / m
    sq = bw <= k.rmin(bw, n) * 1.05
    sq = k.shift(sq.astype(xp.float64), 1, fill=0.0) > 0  # shift(1); first bar False like fillna(False)
    return k.state(sq & (d.c > m), sq & (d.c < m), hold=hold)


def b_inside_bar(k, d, hold):
    h1, l1 = k.shift(d.h, 1), k.shift(d.l, 1)
    inside = (h1 < k.shift(d.h, 2)) & (l1 > k.shift(d.l, 2))
    return k.state(inside & (d.c > h1), inside & (d.c < l1), hold=hold)


def b_dual_momentum(k, d, short, long):
    c = d.c
    return k.sign0((c / k.shift(c, short) - 1) - (c / k.shift(c, long) - 1) * short / long)


def b_jev_proxy(k, d, z_open, z_exit):
    vol = k.rstd(d.c / k.shift(d.c, 1) - 1, 30)
    z = (d.c / k.shift(d.c, 20) - 1) / (vol * math.sqrt(20))
    return k.state(z >= z_open, z <= -z_open, exit_=k.xp.abs(z) < z_exit)


# Map reference param names -> batched arg names (Python can't take `k` twice).
_RENAME = {"k": "k_"}

BFAMILIES = {
    "sma_cross": b_sma_cross, "ema_cross": b_ema_cross, "price_vs_sma": b_price_vs_sma,
    "ma_slope": b_ma_slope, "tsmom": b_tsmom, "reversal": b_reversal, "donchian": b_donchian,
    "bb_revert": b_bb_revert, "bb_breakout": b_bb_breakout, "rsi_revert": b_rsi_revert,
    "rsi_momentum": b_rsi_momentum, "macd": b_macd, "zscore_revert": b_zscore_revert,
    "stoch_revert": b_stoch_revert, "keltner_breakout": b_keltner_breakout, "streak": b_streak,
    "near_high": b_near_high, "volume_spike": b_volume_spike, "day_of_week": b_day_of_week,
    "vol_squeeze": b_vol_squeeze, "inside_bar": b_inside_bar, "dual_momentum": b_dual_momentum,
    "jev_proxy": b_jev_proxy,
}


# ---------------------------------------------------------------- the chart batch

@dataclass
class DeviceBatch:
    o: object
    h: object
    l: object
    c: object
    v: object
    dow_next: object
    above: object      # close > SMA200
    trend_ok: object   # SMA200 defined
    lowvol: object     # ATR% < its 250-bar median
    vol_ok: object
    t_idx: object      # (T, 1) bar index
    start: object      # (1, B) first evaluated bar
    end: object        # (1, B) one past last evaluated bar


class HostBatch:
    """Charts padded to a common length, stored time-major on the host.

    Columns are grouped: group 0 = the real charts, group g>0 = shuffled replicate g-1;
    each group has `n_charts` columns in chart order.
    """

    def __init__(self, segments: list[tuple[pd.DataFrame, int]], n_charts: int):
        self.n_charts = n_charts
        self.n_groups = len(segments) // n_charts
        T = max(len(s) for s, _ in segments)
        B = len(segments)
        self.T, self.B = T, B
        f = {k: np.empty((T, B)) for k in ("o", "h", "l", "c", "v")}
        dow = np.full((T, B), -1, dtype=np.int8)
        self.start = np.empty(B, dtype=np.int64)
        self.end = np.empty(B, dtype=np.int64)
        for j, (seg, st) in enumerate(segments):
            n = len(seg)
            for k, col in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume")):
                a = seg[col].to_numpy()
                f[k][:n, j] = a
                f[k][n:, j] = a[-1]  # right-pad with a flat copy of the last bar: never evaluated
            dow[:n - 1, j] = seg.index.dayofweek[1:]
            self.start[j], self.end[j] = st, n
        self.f = f
        self.dow = dow
        self._filters()

    def _filters(self):
        # Regime filters depend only on the chart, not the strategy: compute once, in pandas,
        # with exactly the reference definitions in signals._filter_mask.
        c = pd.DataFrame(self.f["c"])
        h, l = pd.DataFrame(self.f["h"]), pd.DataFrame(self.f["l"])
        m = c.rolling(200, min_periods=200).mean()
        self.above = (c > m).to_numpy()
        self.trend_ok = m.notna().to_numpy()
        pc = c.shift(1)
        tr = np.fmax(np.fmax((h - l).to_numpy(), (h - pc).abs().to_numpy()), (l - pc).abs().to_numpy())
        a = pd.DataFrame(tr).ewm(alpha=1 / 20, adjust=False, min_periods=20).mean() / c
        med = a.rolling(250, min_periods=100).median()
        self.vol_ok = (a.notna() & med.notna()).to_numpy()
        self.lowvol = (a < med).to_numpy()

    def column_chunks(self, max_cols: int) -> list[slice]:
        """Column slices on group boundaries, each at most max_cols wide (>= one group)."""
        per = max(1, max_cols // self.n_charts) * self.n_charts
        return [slice(i, min(i + per, self.B)) for i in range(0, self.B, per)]

    def to_device(self, ops: Ops, cols: slice) -> DeviceBatch:
        a = ops.asarray
        f = self.f
        return DeviceBatch(
            a(np.ascontiguousarray(f["o"][:, cols])), a(np.ascontiguousarray(f["h"][:, cols])),
            a(np.ascontiguousarray(f["l"][:, cols])), a(np.ascontiguousarray(f["c"][:, cols])),
            a(np.ascontiguousarray(f["v"][:, cols])), a(np.ascontiguousarray(self.dow[:, cols])),
            a(np.ascontiguousarray(self.above[:, cols])), a(np.ascontiguousarray(self.trend_ok[:, cols])),
            a(np.ascontiguousarray(self.lowvol[:, cols])), a(np.ascontiguousarray(self.vol_ok[:, cols])),
            a(np.arange(self.T)[:, None]), a(self.start[cols][None, :]), a(self.end[cols][None, :]))


# Peak simultaneous (T, B) float64 temporaries for the heaviest strategy: measured 10.1 with
# tracemalloc on the CPU path (zscore_revert); padded for the CuPy pool's block rounding.
PEAK_ARRAYS = 16
RESIDENT_BYTES_PER_CELL = 5 * 8 + 1 + 4  # o,h,l,c,v float64 + dow int8 + 4 bool masks


def positions(ops: Ops, d: DeviceBatch, strat) -> object:
    xp = ops.xp
    params = {_RENAME.get(k, k): v for k, v in strat.params}
    # The flat right-padding has zero variance -> x/0 there. Those bars are never evaluated.
    with np.errstate(divide="ignore", invalid="ignore"):
        pos = BFAMILIES[strat.family](ops, d, **params)
    if strat.direction == "long":
        pos = xp.clip(pos, 0, 1)
    elif strat.direction == "short":
        pos = xp.clip(pos, -1, 0)
    if strat.filter == "trend":
        keep = d.trend_ok & (((pos > 0) & d.above) | ((pos < 0) & ~d.above))
    elif strat.filter == "lowvol":
        keep = d.vol_ok & d.lowvol
    elif strat.filter == "highvol":
        keep = d.vol_ok & ~d.lowvol
    else:
        return pos
    return xp.where(keep, pos, 0.0)


def net_returns(ops: Ops, d: DeviceBatch, pos, cost: float):
    """Same accounting as backtest.strategy_returns, for every column; zero outside [start, end)."""
    xp = ops.xp
    oo = xp.zeros_like(d.o)
    oo[:-1] = d.o[1:] / d.o[:-1] - 1.0
    oo = xp.where(d.t_idx == d.end - 1, 0.0, oo)  # each chart's last forward interval is unknown
    held = ops.shift(pos, 1, fill=0.0)
    prev = ops.shift(held, 1, fill=0.0)
    prev = xp.where(d.t_idx == d.start, 0.0, prev)  # flat entering the evaluation window
    mask = (d.t_idx >= d.start) & (d.t_idx < d.end)
    net = xp.where(mask, held * oo - cost * xp.abs(held - prev), 0.0)
    entries = xp.sum(mask & (held != 0) & (held != prev), axis=0)
    in_mkt = xp.sum(mask & (held != 0), axis=0)
    return net, mask, entries, in_mkt


def group_sharpes(ops: Ops, net, mask, n_charts: int, bpy: float) -> np.ndarray:
    """Pooled annualized Sharpe of each group of n_charts columns (concatenated returns)."""
    xp = ops.xp
    T, B = net.shape
    g = B // n_charts
    s = xp.sum(net.reshape(T, g, n_charts), axis=(0, 2))
    s2 = xp.sum((net * net).reshape(T, g, n_charts), axis=(0, 2))
    n = xp.sum(mask.reshape(T, g, n_charts), axis=(0, 2)).astype(xp.float64)
    mu = s / n
    var = (s2 - s * mu) / (n - 1)
    sd = xp.sqrt(xp.maximum(var, 0.0))
    sr = xp.where(sd > 0, mu / xp.where(sd > 0, sd, 1.0), 0.0) * math.sqrt(bpy)
    return ops.to_host(sr)
