"""python -m edgelab  — search every edge, keep only what survives random unseen charts.

Examples
  python -m edgelab --csv-dir ~/data/daily                    # your own OHLCV CSVs, one per instrument
  python -m edgelab --binance BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,ADAUSDT --interval 1d
  python -m edgelab --demo noise                               # sanity check: must find nothing
"""
from __future__ import annotations

import argparse
import sys

from . import data
from .pipeline import Config, run
from .signals import FAMILIES


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="edgelab", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv-dir", help="directory of <SYMBOL>.csv files (date,open,high,low,close[,volume])")
    src.add_argument("--binance", help="comma-separated Binance spot symbols, fetched from the public API")
    src.add_argument("--demo", choices=["edge", "noise"], help="synthetic check: 'edge' (planted edge, expect keepers) or 'noise' (random walks, expect none)")
    ap.add_argument("--interval", default="1d", help="Binance kline interval (default 1d)")
    ap.add_argument("--start", default="2017-01-01", help="Binance history start date")
    ap.add_argument("--save-data", help="with --binance: also save the fetched CSVs here")
    ap.add_argument("--out", default="edgelab_results", help="output directory")
    ap.add_argument("--families", help=f"subset of: {','.join(FAMILIES)}")
    d = Config()
    for name, val in vars(d).items():
        if name in ("families",) or isinstance(val, list):
            continue
        typ = type(val) if val is not None else float
        ap.add_argument("--" + name.replace("_", "-"), type=typ, default=val)
    a = ap.parse_args(argv)

    cfg = Config(**{k: getattr(a, k) for k in vars(d) if k != "families"})
    cfg.families = a.families.split(",") if a.families else None

    if a.csv_dir:
        universe = data.load_dir(a.csv_dir)
    elif a.binance:
        universe = {}
        for sym in a.binance.split(","):
            print(f"fetching {sym} {a.interval} ...", file=sys.stderr)
            universe[sym] = data.fetch_binance(sym.strip().upper(), a.interval, a.start)
            if a.save_data:
                from pathlib import Path
                Path(a.save_data).mkdir(parents=True, exist_ok=True)
                universe[sym].to_csv(Path(a.save_data) / f"{sym}.csv", index_label="date")
    else:
        universe = demo_universe(cfg.seed, kind=a.demo)

    res = run(universe, cfg, a.out)
    for k in res["keepers"]:
        print(f"KEEP  {k['id']}  OOS Sharpe {k['oos_sharpe']:.2f}  trades {int(k['oos_trades'])}  "
              f"noise p={k['noise_p']:.3f}")
    return 0


def demo_universe(seed: int = 7, n: int = 2500, kind: str = "edge") -> dict:
    """kind='edge': every instrument has planted short-term mean reversion -> expect keepers.
    kind='noise': pure random walks with drift -> any keeper is a false positive."""
    if kind == "noise":
        return {f"RW{i}": data.random_walk(n, seed=seed * 100 + i, drift=0.0003) for i in range(6)}
    return {f"MR{i}": data.planted_edge(n, seed=seed * 100 + 50 + i, phi=-0.2) for i in range(6)}


if __name__ == "__main__":
    raise SystemExit(main())
