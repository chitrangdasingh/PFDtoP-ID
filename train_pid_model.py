"""
train_pid_model.py - train the PFD -> P&ID model in three stages and compare against ablations.

  stage 1  pre-train on the synthetic P&ID graphs (GraphML, PID2Graph format): pipe-run grammar, coarse tokens
  stage 2  train on generated flowsheets (random plants labelled by an engineering policy)
  stage 3  fine-tune on the real plant: the 10 TPD Aspen case (data/streams_aspen_10tpd.csv)
  test     the 8.5 TPD PFD-basis case (data/streams_gasifier_full.csv), never used in training, plus held-out generated plants

Run:   python train_pid_model.py --graphml data/synthetic_pid/*.graphml
Writes models/pid_model.npz (+ .json) and models/training_report.json
"""
import argparse
import glob
import json
import random
import time
from pathlib import Path

import numpy as np
import pandas as pd

import pfd_to_pid as p
import pid_ai as A

HERE = Path(__file__).parent


def real_elements(case, augment=0, rng=None):
    eq = pd.read_csv(p.DATA / "equipment.csv")
    st = pd.read_csv(p.DATA / case, dtype={"no": str})
    el = A.elements(eq, st)
    if not augment:
        return el
    out = list(el)
    drop_pref = ("tband=", "fromkw=", "tokw=", "kw=", "from_out=", "to_in=", "nin=", "nout=", "in_svc=", "out_svc=")
    for _ in range(augment):                     # augmentation: hide one or two context features at random
        for kind, ident, base, tgt in el:
            cand = [f for f in base if f.startswith(drop_pref)]
            gone = set(rng.sample(cand, min(len(cand), rng.choice([1, 2])))) if cand else set()
            out.append((kind, ident, [f for f in base if f not in gone], tgt))
    return out


def matrices(model, els, grow, fine=True):
    X, yf, yc = A.rows_from(els, model.fv, model.tv, model.cv, grow=grow, fine=fine)
    key = {}
    for i, (x, f, c) in enumerate(zip(X, yf, yc)):    # identical rows are merged and weighted
        key.setdefault((x, int(f), int(c)), [i, 0])[1] += 1
    idx = [v[0] for v in key.values()]
    w = np.array([v[1] for v in key.values()], np.float32)
    return [X[i] for i in idx], yf[idx], yc[idx], w


def run_stage(model, els, steps, lr, fine=True, dropout=0.0, seed=0, batch=256):
    """Train for a fixed number of optimiser steps, whatever the number of distinct rows."""
    X, yf, yc, w = matrices(model, els, grow=True, fine=fine)
    model.net.grow_input(len(model.fv))
    model.net.grow_output(len(model.tv))
    epochs = max(1, round(steps * batch / len(X)))
    return model.net.fit(A.densify(X, len(model.fv)), yf, yc, w, epochs=epochs, lr=lr, dropout=dropout, seed=seed, batch=batch)


def new_model(seed=0):
    m = A.PIDModel()
    m.net = A.Net(1, 1, len(m.cv), seed=seed)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--graphml", nargs="*", default=sorted(glob.glob(str(HERE / "data" / "synthetic_pid" / "*.graphml"))))
    ap.add_argument("--plants", type=int, default=1500, help="generated flowsheets for stage 2")
    ap.add_argument("--test-plants", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    t0 = time.time()
    rng = random.Random(a.seed)

    frags = [f for g in a.graphml for f in A.graphml_fragments(g)]
    graph_els = [("line", "g", base, seq) for base, seq in frags]
    gen = [A.random_plant(random.Random(a.seed * 100000 + i)) for i in range(a.plants)]
    gen_els = [e for eq, st in gen for e in A.elements(eq, st)]
    test_gen = [A.random_plant(random.Random(999000 + i)) for i in range(a.test_plants)]
    test_gen_els = [e for eq, st in test_gen for e in A.elements(eq, st)]
    real_train = real_elements("streams_aspen_10tpd.csv", augment=40, rng=rng)
    real_train_plain = real_elements("streams_aspen_10tpd.csv")
    real_test = real_elements("streams_gasifier_full.csv")
    print(f"data: {len(frags)} GraphML fragments, {len(gen)} generated plants ({len(gen_els)} elements), "
          f"real train {len(real_train_plain)} elements (x41 augmented), real test {len(real_test)} elements  [{time.time() - t0:.0f}s]")

    S1 = dict(steps=1500, lr=2e-3)
    S2 = dict(steps=6000, lr=1e-3)
    S3 = dict(steps=800, lr=3e-4, dropout=0.05)

    def stage1(m):
        return run_stage(m, graph_els, fine=False, **S1)

    def stage2(m):
        return run_stage(m, gen_els, **S2)

    def stage3(m):
        return run_stage(m, real_train, seed=3, **S3)

    variants = {
        "A. real plant only (no pre-training)": [stage3],
        "B. generated data only": [stage2],
        "C. GraphML pre-training + generated data": [stage1, stage2],
        "D. generated data + fine-tune on real plant": [stage2, stage3],
        "E. full: GraphML + generated + fine-tune": [stage1, stage2, stage3],
    }
    report, best = {}, None
    for name, stages in variants.items():
        m = new_model(seed=a.seed)
        losses = [s(m)[-1] for s in stages]
        ev = dict(test_real_8p5tpd=A.evaluate(m, real_test), test_generated=A.evaluate(m, test_gen_els), train_real_10tpd=A.evaluate(m, real_train_plain))
        report[name] = dict(final_losses=[round(float(x), 4) for x in losses], **ev)
        print(f"{name}: real test {ev['test_real_8p5tpd']}  generated test {ev['test_generated']}  [{time.time() - t0:.0f}s]")
        if name.startswith("E."):
            best = m
    best.card = dict(
        task="PFD element -> P&ID items (in-line valves and instruments per stream; instruments per equipment item)",
        stages=["1: pre-train on synthetic P&ID graphs (GraphML) - coarse grammar", f"2: {a.plants} generated flowsheets",
                "3: fine-tune on the 10 TPD Aspen case (augmented)"],
        data=dict(graphml_files=[Path(g).name for g in a.graphml], graphml_fragments=len(frags), generated_plants=a.plants,
                  generated_elements=len(gen_els), real_train_elements=len(real_train_plain), real_test_elements=len(real_test)),
        results=report, features=len(best.fv), tokens=best.tv.itos, trained_seconds=round(time.time() - t0))
    best.save(A.MODEL_DIR / "pid_model.npz")
    (A.MODEL_DIR / "training_report.json").write_text(json.dumps(best.card, indent=1), encoding="utf-8")
    print("saved models/pid_model.npz", f"[{time.time() - t0:.0f}s]")


if __name__ == "__main__":
    main()
