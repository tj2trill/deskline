"""Discovery -> out-of-sample gauntlet -> keep or discard.

Stage 1, discovery (in-sample):
  Every generated strategy is backtested on the discovery data: the first `is_frac` of history
  of the discovery instruments. Thousands of trials guarantee some look great by luck, so the
  ranking statistic is the Deflated Sharpe Ratio, which discounts for the number of trials.

Stage 2, gauntlet (out-of-sample, random charts):
  Survivors are tested on randomly drawn charts that discovery never saw:
    - real charts: random windows from held-out instruments (never seen at all) and from the
      unseen later history of the discovery instruments;
    - random charts: each of those windows with its bars shuffled in time (same volatility,
      tails and drift, no serial structure), repeated `perms` times.
  A keeper must make money on the real unseen charts AND do clearly better there than on the
  shuffled charts (permutation p-value, FDR-controlled across all survivors).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from multiprocessing import get_context
from pathlib import Path

import numpy as np
import pandas as pd

from . import backtest as bt
from . import batched as bx
from .data import bars_per_year, permute_bars
from .signals import Strategy, generate

WARMUP = 300  # bars of history needed before the longest indicator (250-bar lookbacks) is valid


@dataclass
class Config:
    cost_bps: float = 10.0          # fee per side, basis points of traded notional
    slippage_bps: float = 5.0       # extra per side
    is_frac: float = 0.6            # share of each discovery instrument's history used for discovery
    holdout_frac: float = 0.3       # share of instruments never seen in discovery (needs >= 3 instruments)
    # stage 1
    min_is_trades: int = 30
    min_is_sharpe: float = 0.5
    min_is_frac_positive: float = 0.5   # share of discovery instruments with positive Sharpe
    min_exposure: float = 0.02
    min_dsr: float = 0.50           # 0.5 = beats the expected luckiest of N skill-less strategies
    max_forward: int = 30
    # stage 2
    charts: int = 30                # random out-of-sample windows
    window: int = 365               # bars per window
    perms: int = 100                # shuffled versions of each window for the noise test
    min_oos_trades: int = 30
    min_oos_sharpe: float = 0.5
    min_frac_profitable: float = 0.55
    fdr_q: float = 0.10
    # run
    seed: int = 7
    workers: int = field(default_factory=lambda: os.cpu_count() or 1)
    bars_per_year: float | None = None
    families: list[str] | None = None
    # stage-2 compute: auto = GPU if CuPy sees one and the batch is big enough, else batched CPU
    device: str = "auto"            # auto | gpu | cpu | pandas (slow reference implementation)
    gpu_min_cells: int = 500_000    # bars x charts below which the GPU is not worth it
    mem_gb: float = 0.0             # stage-2 memory budget per process; 0 = 70% of free VRAM / 1 GB per CPU worker

    @property
    def cost(self) -> float:
        return (self.cost_bps + self.slippage_bps) / 1e4


# ---------------------------------------------------------------- data splitting

@dataclass
class Split:
    discovery: dict[str, pd.DataFrame]          # truncated in-sample frames
    oos_regions: list[tuple[str, int, int]]     # (instrument, first eligible bar, end bar)
    holdout_names: list[str]


def make_split(universe: dict[str, pd.DataFrame], cfg: Config) -> Split:
    rng = np.random.default_rng(cfg.seed)
    names = sorted(universe)
    n_hold = int(round(len(names) * cfg.holdout_frac)) if len(names) >= 3 else 0
    n_hold = min(max(n_hold, 1 if len(names) >= 3 else 0), len(names) - 1)
    holdout = sorted(rng.choice(names, size=n_hold, replace=False).tolist()) if n_hold else []
    discovery, regions = {}, []
    for name in names:
        df = universe[name]
        if name in holdout:
            if len(df) >= WARMUP + cfg.window // 2:
                regions.append((name, WARMUP, len(df)))
            continue
        cut = int(len(df) * cfg.is_frac)
        if cut < WARMUP + 100:
            raise ValueError(f"{name}: only {len(df)} bars; discovery needs > {WARMUP + 100} "
                             f"after the is_frac={cfg.is_frac} cut. Use longer history.")
        discovery[name] = df.iloc[:cut]
        if len(df) - cut >= cfg.window // 2:
            regions.append((name, cut, len(df)))  # warm-up may reach back into discovery bars: features only
    if not discovery:
        raise ValueError("no discovery instruments")
    if not regions:
        raise ValueError("no out-of-sample data left; lower is_frac or window")
    return Split(discovery, regions, holdout)


def sample_charts(universe, split: Split, cfg: Config) -> list[tuple[str, int, int]]:
    """Random (instrument, eval_start, eval_end) windows, weighted by available length."""
    rng = np.random.default_rng(cfg.seed + 1)
    lens = np.array([max(e - s - cfg.window // 2, 1) for _, s, e in split.oos_regions], dtype=float)
    out = []
    for _ in range(cfg.charts):
        name, s, e = split.oos_regions[rng.choice(len(lens), p=lens / lens.sum())]
        length = min(cfg.window, e - s)
        a = int(rng.integers(s, e - length + 1))
        out.append((name, a, a + length))
    return out


# ---------------------------------------------------------------- worker side

_G: dict = {}


def _init(universe, split, charts, cfg):
    _G.update(universe=universe, split=split, charts=charts, cfg=cfg)
    _G.pop("ops", None)  # never inherit a parent's device handles across fork
    _G.pop("dev_cache", None)


def _discover_one(strat: Strategy) -> dict:
    cfg: Config = _G["cfg"]
    rets, sharpes, trades, expo = [], [], 0, []
    fp = hashlib.blake2b(digest_size=12)
    for df in _G["split"].discovery.values():
        pos = strat.positions(df)
        fp.update(pos[WARMUP:].astype(np.int8).tobytes())
        r, n_tr, ex = bt.strategy_returns(df, pos, cfg.cost, start=WARMUP)
        rets.append(r)
        trades += n_tr
        expo.append(ex)
        s = bt.stats(r, n_tr, ex, cfg.bars_per_year)
        sharpes.append(s.sharpe if n_tr > 0 else 0.0)
    pooled = bt.stats(np.concatenate(rets), trades, float(np.mean(expo)), cfg.bars_per_year)
    return {"id": strat.id, "fingerprint": fp.hexdigest(), **pooled.as_dict("is_"),
            "is_frac_positive": float(np.mean([s > 0 for s in sharpes])),
            "is_mean_inst_sharpe": float(np.mean(sharpes))}


def _chart_frame(name, a, b):
    return _chart_frame_from(_G["universe"], name, a, b)


def _chart_returns(strat: Strategy, seg: pd.DataFrame, start: int, cost: float):
    return bt.strategy_returns(seg, strat.positions(seg), cost, start=start)


def _gauntlet_one(sid: str) -> dict:
    cfg: Config = _G["cfg"]
    strat = Strategy.from_id(sid)
    real_r, chart_rets, trades, expo = [], [], 0, []
    segs = []
    for name, a, b in _G["charts"]:
        seg, start = _chart_frame(name, a, b)
        segs.append((seg, start))
        r, n_tr, ex = _chart_returns(strat, seg, start, cfg.cost)
        real_r.append(r)
        trades += n_tr
        expo.append(ex)
        if n_tr > 0:
            chart_rets.append(float(np.prod(1 + r) - 1))
    pooled = bt.stats(np.concatenate(real_r), trades, float(np.mean(expo)), cfg.bars_per_year)

    # Noise test: same windows, bars shuffled. Seeds depend only on (rep, chart), so every
    # strategy faces the identical set of random charts.
    null = np.empty(cfg.perms)
    for rep in range(cfg.perms):
        rs = []
        for j, (seg, start) in enumerate(segs):
            rng = np.random.default_rng([cfg.seed, 1000 + rep, j])
            pseg = permute_bars(seg, rng, keep_first=start)
            rs.append(_chart_returns(strat, pseg, start, cfg.cost)[0])
        null[rep] = bt.stats(np.concatenate(rs), 0, 0, cfg.bars_per_year).sharpe
    p = float((1 + np.sum(null >= pooled.sharpe)) / (cfg.perms + 1))
    return {"id": sid, **pooled.as_dict("oos_"),
            "oos_charts_traded": len(chart_rets),
            "oos_frac_profitable": float(np.mean([x > 0 for x in chart_rets])) if chart_rets else 0.0,
            "noise_sharpe_mean": float(null.mean()), "noise_sharpe_p95": float(np.quantile(null, 0.95)),
            "noise_p": p}


# ---------------------------------------------------------------- batched stage 2

def build_batch(universe, charts, cfg: Config) -> bx.HostBatch:
    """Real windows + every shuffled replicate, padded into one (T, B) batch, built once.
    Uses the same per-(rep, chart) seeds as the reference path, so the random charts are identical."""
    real = [_chart_frame_from(universe, n, a, b) for n, a, b in charts]
    segs = list(real)
    for rep in range(cfg.perms):
        for j, (seg, start) in enumerate(real):
            rng = np.random.default_rng([cfg.seed, 1000 + rep, j])
            segs.append((permute_bars(seg, rng, keep_first=start), start))
    return bx.HostBatch(segs, n_charts=len(charts))


def _chart_frame_from(universe, name, a, b):
    df = universe[name]
    lo = max(a - WARMUP, 0)
    return df.iloc[lo:b], a - lo


def _plan_chunks(hb: bx.HostBatch, device: str, cfg: Config) -> tuple[list[slice], bool]:
    """Split columns so one strategy's temporaries fit the memory budget; keep the data resident
    on the device when it fits alongside them."""
    if cfg.mem_gb > 0:
        budget = cfg.mem_gb * 2**30
    elif device == "gpu":
        import cupy
        free, _ = cupy.cuda.runtime.memGetInfo()
        budget = 0.7 * free
    else:
        budget = 2**30
    resident = hb.T * hb.B * bx.RESIDENT_BYTES_PER_CELL
    per_col_work = hb.T * 8 * bx.PEAK_ARRAYS
    keep = resident < 0.5 * budget
    avail = budget - (resident if keep else 0)
    max_cols = max(int(avail // (per_col_work + (0 if keep else hb.T * bx.RESIDENT_BYTES_PER_CELL))), hb.n_charts)
    return hb.column_chunks(max_cols), keep


def _calibrate(hb: bx.HostBatch, ids: list[str], cfg: Config, log) -> str:
    """Time a couple of the real strategies on one GPU vs one CPU core, on the first chunk of
    the real batch, then compare projected wall time: the CPU path runs min(workers, #strategies)
    strategies in parallel, the GPU path runs them one after another."""
    import cupy
    sample = [Strategy.from_id(i) for i in ids[:2]]
    per = {}
    for dev in ("gpu", "cpu"):
        ops = bx.Ops(dev)
        chunks, _ = _plan_chunks(hb, dev, cfg)
        d = hb.to_device(ops, chunks[0])
        bx.positions(ops, d, sample[0])  # warm-up: NVRTC compile, memory pool
        cupy.cuda.Device().synchronize()
        t = time.perf_counter()
        for s in sample:
            net, mask, _, _ = bx.net_returns(ops, d, bx.positions(ops, d, s), cfg.cost)
            bx.group_sharpes(ops, net, mask, hb.n_charts, cfg.bars_per_year)
        cupy.cuda.Device().synchronize()
        per[dev] = (time.perf_counter() - t) / len(sample) * len(chunks)
        del d
    parallel = max(1, min(cfg.workers, len(ids)))
    est_gpu = per["gpu"] * len(ids)
    est_cpu = per["cpu"] * len(ids) / parallel
    choice = "gpu" if est_gpu < est_cpu else "cpu"
    log(f"stage 2: auto-calibration: GPU {per['gpu'] * 1e3:.0f} ms/strategy vs CPU {per['cpu'] * 1e3:.0f} ms "
        f"x {parallel} parallel -> est. {est_gpu:.0f}s vs {est_cpu:.0f}s, using {choice}")
    return choice


def _gauntlet_batched(sid: str) -> dict:
    cfg: Config = _G["cfg"]
    hb: bx.HostBatch = _G["hb"]
    if "ops" not in _G:
        _G["ops"] = bx.Ops(_G["device"])
        _G["dev_cache"] = {}
    ops = _G["ops"]
    strat = Strategy.from_id(sid)
    C = hb.n_charts
    srs = []
    for i, cols in enumerate(_G["chunks"]):
        d = _G["dev_cache"].get(i) or hb.to_device(ops, cols)
        if _G["keep_resident"]:
            _G["dev_cache"][i] = d
        pos = bx.positions(ops, d, strat)
        net, mask, entries, in_mkt = bx.net_returns(ops, d, pos, cfg.cost)
        srs.append(bx.group_sharpes(ops, net, mask, C, cfg.bars_per_year))
        if i == 0:  # group 0 = the real charts
            real_net = ops.to_host(net[:, :C])
            ent = ops.to_host(entries[:C])
            inm = ops.to_host(in_mkt[:C])
        del pos, net, mask, entries, in_mkt
    sr = np.concatenate(srs)
    real_r, chart_rets, expo = [], [], []
    for j in range(C):
        st, en = int(hb.start[j]), int(hb.end[j])
        r = real_net[st:en, j]
        real_r.append(r)
        expo.append(inm[j] / max(en - st, 1))
        if ent[j] > 0:
            chart_rets.append(float(np.prod(1 + r) - 1))
    trades = int(ent.sum())
    pooled = bt.stats(np.concatenate(real_r), trades, float(np.mean(expo)), cfg.bars_per_year)
    null = sr[1:]
    p = float((1 + np.sum(null >= sr[0])) / (len(null) + 1))
    return {"id": sid, **pooled.as_dict("oos_"),
            "oos_charts_traded": len(chart_rets),
            "oos_frac_profitable": float(np.mean([x > 0 for x in chart_rets])) if chart_rets else 0.0,
            "noise_sharpe_mean": float(null.mean()), "noise_sharpe_p95": float(np.quantile(null, 0.95)),
            "noise_p": p}


def stage2(universe, split, charts, cfg: Config, ids: list[str], log=print) -> pd.DataFrame:
    if cfg.device == "pandas":
        pool = _pool(universe, split, charts, cfg)
        try:
            return pd.DataFrame(_map(pool, _gauntlet_one, ids))
        finally:
            if pool is not None:
                pool.shutdown()
    t0 = time.time()
    hb = build_batch(universe, charts, cfg)
    device = bx.choose_device(cfg.device, hb.T * hb.B, cfg.gpu_min_cells)
    if cfg.device == "auto" and device == "gpu":
        device = _calibrate(hb, ids, cfg, log)
    chunks, keep = _plan_chunks(hb, device, cfg)
    log(f"stage 2: batch {hb.T} bars x {hb.B} charts built in {time.time() - t0:.1f}s -> device={device}, "
        f"{len(chunks)} chunk(s){', resident' if keep else ', streamed'}")
    extra = {"hb": hb, "device": device, "chunks": chunks, "keep_resident": keep}
    if device == "gpu":  # one GPU: run in this process, data stays resident across strategies
        _G.clear()
        _init(universe, split, charts, cfg)
        _G.update(extra)
        try:
            return pd.DataFrame([_gauntlet_batched(i) for i in ids])
        finally:
            _G.clear()
    _G.clear()
    _G.update(extra)  # set before fork so workers share the batch copy-on-write
    pool = _pool(universe, split, charts, cfg)
    try:
        return pd.DataFrame(_map(pool, _gauntlet_batched, ids))
    finally:
        if pool is not None:
            pool.shutdown()
        _G.clear()


def _pool(universe, split, charts, cfg):
    if cfg.workers <= 1:
        _init(universe, split, charts, cfg)
        return None
    # fork: workers inherit the data copy-on-write instead of pickling it per task (Linux/macOS)
    return ProcessPoolExecutor(cfg.workers, mp_context=get_context("fork"),
                               initializer=_init, initargs=(universe, split, charts, cfg))


def _map(pool, fn, items):
    if pool is None:
        return [fn(x) for x in items]
    chunk = max(1, len(items) // (pool._max_workers * 8))
    return list(pool.map(fn, items, chunksize=chunk))


# ---------------------------------------------------------------- driver

def run(universe: dict[str, pd.DataFrame], cfg: Config, out_dir: str | Path | None = None,
        log=print) -> dict:
    if cfg.bars_per_year is None:
        cfg.bars_per_year = float(np.median([bars_per_year(df) for df in universe.values()]))
    split = make_split(universe, cfg)
    charts = sample_charts(universe, split, cfg)
    strategies = generate(cfg.families)
    log(f"universe: {len(universe)} instruments | discovery: {list(split.discovery)} | "
        f"never-seen holdout: {split.holdout_names or 'none (need >= 3 instruments)'}")
    log(f"bars/year={cfg.bars_per_year:.1f}  cost={cfg.cost * 1e4:.1f} bps/side  workers={cfg.workers}")
    log(f"stage 1: testing {len(strategies)} strategies in-sample ...")

    pool = _pool(universe, split, charts, cfg)
    try:
        disc = pd.DataFrame(_map(pool, _discover_one, strategies))
        # Different parameters can produce the exact same trades; count them once (also for N).
        dup = disc.duplicated("fingerprint", keep="first")
        disc["duplicate_of"] = disc["fingerprint"].map(disc[~dup].set_index("fingerprint")["id"]).where(dup, "")
        n_distinct = int((~dup).sum())
        disc["is_dsr"] = [bt.deflated_sharpe(r.is_sr_bar, int(r.is_n), r.is_skew, r.is_kurt, n_distinct)
                          for r in disc.itertuples()]
        disc["stage1_reason"] = disc.apply(lambda r: _stage1_reason(r, cfg), axis=1)
        disc.loc[dup, "stage1_reason"] = "duplicate of " + disc.loc[dup, "duplicate_of"]
        passed = disc[disc["stage1_reason"] == ""].sort_values("is_dsr", ascending=False)
        forwarded = passed.head(cfg.max_forward)
        log(f"stage 1: {n_distinct} distinct after removing identical-trade duplicates")
        log(f"stage 1: {len(passed)} passed, forwarding top {len(forwarded)} by deflated Sharpe")

        if len(forwarded):
            log(f"stage 2: {len(charts)} random unseen charts x ({cfg.perms} shuffled versions each) "
                f"for {len(forwarded)} strategies ...")
            pool.shutdown() if pool is not None else None
            pool = None
            g = stage2(universe, split, charts, cfg, forwarded["id"].tolist(), log)
            g = forwarded[["id", "is_sharpe", "is_dsr", "is_trades", "is_cagr", "is_max_dd"]].merge(g, on="id")
            g["noise_fdr_pass"] = bt.benjamini_hochberg(g["noise_p"].tolist(), cfg.fdr_q)
            g["reject_reason"] = g.apply(lambda r: _stage2_reason(r, cfg), axis=1)
            g["verdict"] = np.where(g["reject_reason"] == "", "KEEP", "DISCARD")
            g = g.sort_values(["verdict", "oos_sharpe"], ascending=[False, False])
        else:
            g = pd.DataFrame(columns=["id", "verdict", "reject_reason"])
    finally:
        if pool is not None:
            pool.shutdown()

    bench = _buy_and_hold(universe, charts, cfg)
    keepers = g[g["verdict"] == "KEEP"]
    log(f"result: {len(keepers)} keeper(s) out of {len(strategies)} strategies tried")
    result = {"config": {k: v for k, v in asdict(cfg).items()}, "n_tried": len(strategies), "n_distinct": n_distinct,
              "n_stage1": int(len(passed)), "n_forwarded": int(len(forwarded)),
              "holdout": split.holdout_names, "discovery": list(split.discovery),
              "charts": [{"instrument": n, "from": str(universe[n].index[a]), "to": str(universe[n].index[b - 1])}
                         for n, a, b in charts],
              "buy_and_hold_oos_sharpe": bench,
              "keepers": keepers.to_dict("records"), "discovery_table": disc, "gauntlet_table": g}
    if out_dir:
        _write(result, Path(out_dir))
        log(f"wrote {out_dir}/report.md, keepers.json, gauntlet.csv, discovery.csv")
    return result


def _stage1_reason(r, cfg: Config) -> str:
    if r.is_trades < cfg.min_is_trades:
        return f"too few trades ({r.is_trades})"
    if r.is_exposure < cfg.min_exposure:
        return "almost never in the market"
    if r.is_sharpe < cfg.min_is_sharpe:
        return f"in-sample Sharpe {r.is_sharpe:.2f} < {cfg.min_is_sharpe}"
    if r.is_frac_positive < cfg.min_is_frac_positive:
        return f"positive on only {r.is_frac_positive:.0%} of instruments"
    if r.is_dsr < cfg.min_dsr:
        return f"deflated Sharpe {r.is_dsr:.2f} < {cfg.min_dsr} (could be luck, given how many were tried)"
    return ""


def _stage2_reason(r, cfg: Config) -> str:
    why = []
    if r.oos_trades < cfg.min_oos_trades:
        why.append(f"only {int(r.oos_trades)} OOS trades")
    if r.oos_sharpe < cfg.min_oos_sharpe:
        why.append(f"OOS Sharpe {r.oos_sharpe:.2f} < {cfg.min_oos_sharpe}")
    if r.oos_frac_profitable < cfg.min_frac_profitable:
        why.append(f"profitable on {r.oos_frac_profitable:.0%} of charts")
    if not r.noise_fdr_pass:
        why.append(f"not distinguishable from shuffled charts (p={r.noise_p:.3f})")
    return "; ".join(why)


def _buy_and_hold(universe, charts, cfg: Config) -> float:
    rs = []
    for name, a, b in charts:
        df = universe[name]
        o = df["open"].to_numpy()[a:b + 1]
        rs.append(o[1:] / o[:-1] - 1)
    r = np.concatenate(rs) if rs else np.zeros(2)
    return bt.stats(r, 1, 1.0, cfg.bars_per_year).sharpe


def _write(res: dict, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    res["discovery_table"].sort_values("is_dsr", ascending=False).to_csv(out / "discovery.csv", index=False)
    res["gauntlet_table"].to_csv(out / "gauntlet.csv", index=False)
    meta = {k: v for k, v in res.items() if not k.endswith("_table")}
    (out / "keepers.json").write_text(json.dumps(meta, indent=2, default=_jsonable))
    (out / "report.md").write_text(report_md(res))


def _jsonable(x):
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        return None if math.isnan(x) else float(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    return str(x)


def report_md(res: dict) -> str:
    c = res["config"]
    g = res["gauntlet_table"]
    lines = [
        "# Edge search report", "",
        f"- Strategies tried: **{res['n_tried']}** ({res['n_distinct']} distinct after merging identical-trade duplicates)",
        f"- Passed in-sample filters: **{res['n_stage1']}** (forwarded top {res['n_forwarded']} by deflated Sharpe)",
        f"- Keepers after out-of-sample gauntlet: **{len(res['keepers'])}**",
        f"- Discovery instruments: {', '.join(res['discovery'])}",
        f"- Never-seen holdout instruments: {', '.join(res['holdout']) or 'none'}",
        f"- OOS: {c['charts']} random windows of {c['window']} bars, each also shuffled {c['perms']}x",
        f"- Costs: {c['cost_bps'] + c['slippage_bps']:.1f} bps per side | buy & hold Sharpe on the same charts: "
        f"{res['buy_and_hold_oos_sharpe']:.2f}", "",
    ]
    cols = ["id", "verdict", "is_sharpe", "is_dsr", "oos_sharpe", "oos_trades", "oos_frac_profitable",
            "oos_max_dd", "noise_sharpe_p95", "noise_p", "reject_reason"]
    if len(g):
        lines += ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        for r in g[cols].itertuples(index=False):
            lines.append("| " + " | ".join(_fmt(v) for v in r) + " |")
    else:
        lines.append("Nothing survived the in-sample stage.")
    lines += ["", "A keeper made money on unseen real charts, on most of them, after costs, and did "
              "significantly better there than on the same charts with their bars shuffled. That is "
              "evidence, not proof: re-run with a different `--seed` and on more instruments before "
              "trading it, and paper-trade first."]
    return "\n".join(lines) + "\n"


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)
