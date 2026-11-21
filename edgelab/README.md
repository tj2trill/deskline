# edgelab — brute-force edge search with a random-chart gauntlet

Tries every strategy in its library, forwards the ones that work in-sample, then tests each
survivor on **random charts it has never seen**. If it holds up, it's a keeper; if not, it's thrown out.

```bash
pip install numpy pandas
python -m edgelab --demo noise        # sanity check: random walks, must find 0 keepers
python -m edgelab --demo edge         # planted mean-reversion: must find only reversion keepers
python -m edgelab --csv-dir ~/data/daily --out results/
python -m edgelab --binance BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,LTCUSDT \
                  --interval 1d --save-data data/binance --out results/
```

CSV format: one file per instrument, a date column plus `open,high,low,close[,volume]`
(Yahoo Finance downloads work as is). Use at least 5 instruments with ~2,000+ bars each so
some can be held out entirely.

## What "every edge" means

22 signal families × parameter grids × direction (long / short / both) × regime filter
(none / above-or-below 200 SMA / low vol / high vol) = **2,724 strategies**. Families:
SMA/EMA crosses, price vs SMA, MA slope, time-series momentum, dual momentum, short-term reversal,
Donchian breakout, Bollinger reversion & breakout, Keltner breakout, RSI reversion & momentum,
stochastic reversion, z-score reversion, MACD, up/down streaks (fade or follow), near 52-week
high/low, volume spikes, day-of-week, volatility squeeze, inside bars.
Add one by writing a function in `signals.py` and registering it in `FAMILIES`.

## The pipeline

1. **Split.** ~30% of instruments are held out entirely. The rest contribute only their
   first 60% of history to discovery.
2. **Stage 1, in-sample.** Every strategy is backtested on the discovery data, with the signal at
   close *t* filled at the open of *t+1* and fees plus slippage charged on every position change.
   Strategies that take identical trades are merged. A strategy passes if it has enough trades,
   a Sharpe ≥ 0.5, is positive on at least half the instruments, and a **Deflated Sharpe Ratio** ≥ 0.5.
   That last bar means it beats the best result you'd expect from that many strategies with no
   skill at all. The top 30 go forward.
3. **Stage 2, random charts.** 30 random windows (365 bars each) are drawn from data discovery
   never saw: held-out instruments, plus the later 40% of the others. For each survivor:
   - **Real unseen charts:** Sharpe ≥ 0.5 after costs, profitable on ≥ 55% of the charts, and ≥ 30 trades.
   - **Truly random charts:** each window is rebuilt 100 times with its bars shuffled in time.
     This keeps the volatility, fat tails and total drift but destroys every pattern a timing rule
     could use. The strategy must beat its own results on those shuffled charts
     (permutation p-value, Benjamini–Hochberg FDR 10% across all survivors).
     Because drift survives the shuffle, a long-only rule can't pass just by riding a bull market.
4. **Verdict.** `KEEP` only if every check passes; otherwise `DISCARD` with the reasons.

Output in `--out`: `report.md`, `keepers.json` (keepers plus the exact charts used), `gauntlet.csv`,
and `discovery.csv` (every strategy's in-sample stats and why it was cut).

Every threshold is a flag: `python -m edgelab --help`. For example `--cost-bps 2 --slippage-bps 1`
for liquid futures, or `--window 252` for stock daily bars.

## Limits

- A keeper is evidence, not proof. Re-run with other `--seed` values (they change the
  holdout and the charts) and on new instruments, then paper-trade it.
- Costs are a flat bps per side. Thin markets and large size cost more.
- Shorts are modelled as frictionless, with no borrow fee or funding rate. On perpetual futures,
  subtract funding yourself.
- Daily bars are the tested path. Intraday works, but fixed indicator lookbacks (e.g. 200)
  then mean something different.
- Run time: about 15 s total for the demo (6 instruments) on 4 CPU cores.

## GPU (optional)

Stage 2 (thousands of real + shuffled charts per strategy) uses a batched engine that runs on
NumPy or, if available, on an NVIDIA GPU through CuPy. Stage 1 stays on the CPU: it only has a
handful of instruments per strategy, too few to fill a GPU.

```bash
pip install cupy-cuda12x          # RTX 50xx (Blackwell, sm_120) needs CUDA 12.8+ and driver >= 570
python -m edgelab.gpucheck        # 1) GPU == CPU for every strategy  2) speed table
python -m edgelab --csv-dir data/ --device auto   # default
```

- `--device auto` (default): if CuPy sees a GPU and the batch is at least `--gpu-min-cells`
  (bars × charts, default 500k), it times two of the real strategies on both devices and picks
  whichever finishes sooner. The CPU path runs `min(workers, #strategies)` strategies in parallel,
  while the GPU runs them one after another, so the GPU has to beat all your cores, not just one.
- `--device gpu` / `--device cpu` force a device. `--device pandas` uses the slow reference
  implementation (for checking).
- `--mem-gb`: memory budget for stage 2. The default is 70% of free VRAM, or 1 GB per CPU worker.
  Charts are split into column chunks to fit it, so larger `--perms` / `--charts` stream instead of
  running out of memory.
- Everything is float64. GeForce cards run FP64 math at 1/64 speed, but this workload is limited by
  memory bandwidth. float32 would make the rolling sums too imprecise for exact crossover comparisons.
