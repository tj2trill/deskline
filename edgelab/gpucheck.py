"""python -m edgelab.gpucheck — verify the GPU engine matches the CPU engine, then time both.

Run this once on a new machine/driver before trusting --device gpu. It checks every strategy
in the library position-for-position, then benchmarks a stage-2-sized batch and reports the
batch size at which the GPU starts to win (use it for --gpu-min-cells).
"""
from __future__ import annotations

import argparse
import time

import numpy as np

from . import batched as bx
from . import data
from .signals import generate


def _segments(n_charts: int, length: int = 665, warmup: int = 300):
    segs = []
    for j in range(n_charts):
        gen = data.planted_edge if j % 2 else data.random_walk
        segs.append((gen(length, seed=j), warmup))
    return segs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--charts", type=int, default=3030, help="benchmark batch width (30 charts x 101 groups)")
    a = ap.parse_args(argv)
    if not bx.gpu_available():
        print("No CUDA device visible to CuPy. Install: pip install cupy-cuda12x  (driver >= 570 for RTX 50xx)")
        return 1
    import cupy
    props = cupy.cuda.runtime.getDeviceProperties(0)
    print(f"GPU: {props['name'].decode()}  sm_{props['major']}{props['minor']}  "
          f"{props['totalGlobalMem'] / 2**30:.1f} GB  CuPy {cupy.__version__}")

    cpu, gpu = bx.Ops("cpu"), bx.Ops("gpu")
    hb = bx.HostBatch(_segments(12), n_charts=12)
    dc, dg = hb.to_device(cpu, slice(0, hb.B)), hb.to_device(gpu, slice(0, hb.B))
    bad = 0
    strategies = generate()
    for s in strategies:
        pc = bx.positions(cpu, dc, s)
        pg = gpu.to_host(bx.positions(gpu, dg, s))
        diff = int(np.sum(pc[:-1] != pg[:-1]))
        if diff:
            bad += 1
            print(f"  MISMATCH {s.id}: {diff} bars")
    print(f"correctness: {len(strategies) - bad}/{len(strategies)} strategies identical on GPU and CPU")

    sample = strategies[::25]
    print(f"\nbenchmark: {len(sample)} strategies, batch 665 bars x N charts (CPU = 1 core)")
    print(f"{'charts':>8} {'cells':>10} {'cpu ms':>9} {'gpu ms':>9} {'speedup':>8}")
    crossover = None
    for n in (30, 300, 1000, a.charts):
        hb = bx.HostBatch(_segments(n), n_charts=min(n, 30) if n % 30 == 0 else n)
        dc, dg = hb.to_device(cpu, slice(0, hb.B)), hb.to_device(gpu, slice(0, hb.B))
        times = {}
        for name, ops, d in (("cpu", cpu, dc), ("gpu", gpu, dg)):
            bx.positions(ops, d, sample[0])  # warm-up: kernel compile, memory pool
            cupy.cuda.Device().synchronize()
            t = time.perf_counter()
            for s in sample:
                net, mask, _, _ = bx.net_returns(ops, d, bx.positions(ops, d, s), 0.0015)
                bx.group_sharpes(ops, net, mask, hb.n_charts, 365.0)
            cupy.cuda.Device().synchronize()
            times[name] = (time.perf_counter() - t) / len(sample) * 1000
        sp = times["cpu"] / times["gpu"]
        if crossover is None and sp > 1.5:
            crossover = hb.T * hb.B
        print(f"{hb.B:>8} {hb.T * hb.B:>10} {times['cpu']:>9.1f} {times['gpu']:>9.1f} {sp:>7.1f}x")
    print("\nNote: stage 2 on CPU runs one strategy per core, so compare GPU speedup against your core count.")
    if crossover:
        print(f"GPU beats one core by >1.5x from ~{crossover} cells (current --gpu-min-cells {bx.GPU_MIN_CELLS}).")
    return 0 if bad == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
