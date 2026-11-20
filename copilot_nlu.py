"""
copilot_nlu.py - the copilot's trained language model for questions without an AI key.

A small intent classifier (softmax regression on hashed word and word-pair features, NumPy only) learns which tool a
question needs ("what if FV-4021 sticks", "which material for the oxidant line", "set point of TIC-4001" ...).
Training data = question templates below filled with the plant's real tags + examples people add in the app
(data/copilot_examples.csv: text, intent). Training takes a few seconds; the model is saved in models/copilot_intents.npz.
Held-out templates (never seen in training) give an honest accuracy figure in models/copilot_intents.json.

Tags in a question are found separately (exact match against the P&ID), so the classifier only decides WHAT is asked.
"""
import json
import random
import re
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
MODEL = HERE / "models" / "copilot_intents.npz"
EXAMPLES = HERE / "data" / "copilot_examples.csv"
DIM = 1 << 14

TEMPLATES = {
    "trace": ["tell me about {x}", "what is {x}", "details of {x}", "show {x}", "where is {x} on the drawing", "which zone is {x} in", "who changed {x}",
              "where does {x} come from", "give me the data of {x}", "describe {x}", "info on {x}", "what do we know about {x}", "trace {x}",
              "history of {x}", "what corrections were made to {x}", "is there any remark on {x}"],
    "failure": ["what if {x} fails", "what happens if {x} fails closed", "what if {x} sticks open", "consequence of {x} failure", "{x} fails what happens",
                "impact if {x} stops", "if {x} trips what is lost", "failure of {x}", "what breaks when {x} is lost", "suppose {x} goes wrong",
                "what if {x} reads high", "effect of losing {x}", "what happens downstream if {x} closes", "fmea for {x}", "{x} spurious trip effect"],
    "path": ["path from {x} to {y}", "how does flow go from {x} to {y}", "route between {x} and {y}", "is {x} connected to {y}", "trace from {x} to {y}",
             "how is {x} linked to {y}", "what is between {x} and {y}", "flow path {x} {y}"],
    "neighbours": ["what is around {x}", "what is connected to {x}", "neighbours of {x}", "items near {x}", "what feeds {x}", "what is downstream of {x}",
                   "what is upstream of {x}", "what does {x} connect to", "which lines go to {x}", "what instruments are on {x}"],
    "setpoint": ["what is the set point of {x}", "alarm limits for {x}", "trip setting of {x}", "what does {x} trip at", "high alarm of {x}",
                 "normal value of {x}", "range of {x}", "what temperature should {x} control", "setpoint {x}", "at what value does {x} alarm",
                 "low low trip of {x}", "what is the operating value at {x}"],
    "moc": ["what material is {x}", "which moc for {x}", "pipe class of {x}", "what is {x} made of", "insulation of {x}", "is {x} insulated",
            "what end connection on {x}", "can {x} be threaded", "design pressure of {x}", "design temperature for {x}", "which steel for {x}",
            "material of construction of the oxidant line", "what moc for hot syngas", "stainless or carbon steel for condensate", "corrosion in {x}",
            "test pressure of {x}", "spec break on {x}", "what gasket for oxygen service"],
    "fail_position": ["fail position of {x}", "is {x} fail open or fail closed", "what does {x} do on air failure", "fc or fo for {x}",
                      "how does {x} fail safe", "actuator of {x}", "what happens to {x} on power loss", "why is {x} fail closed"],
    "nozzles": ["nozzles on {x}", "nozzle schedule of {x}", "what connections does {x} have", "drains on {x}", "vents on {x}", "where is the drain of {x}",
                "manway on {x}", "what size is the inlet nozzle of {x}", "relief nozzle on {x}", "how many nozzles on {x}"],
    "balance": ["what is the cold gas efficiency", "mass balance", "energy balance", "does the mass balance close", "heat loss of the gasifier",
                "how much syngas do we make", "what is the net power", "char conversion", "engine efficiency", "how much ash", "lhv of syngas",
                "corrected balance for a real downdraft", "how much oxygen is needed", "condensate flow", "what is the exhaust flow",
                "how much energy is recovered in the exhaust exchanger", "aspen closure", "overall efficiency of the plant"],
    "bom": ["bill of material", "bom", "how many valves do we need", "list of pipes to buy", "how many instruments", "bom for valves", "material take off",
            "how much pipe", "cost of the plant", "quantities"],
    "check": ["check the drawing", "what is missing", "open remarks", "pending corrections", "where does the model disagree", "review status",
              "what should be reviewed", "what is still open", "show gaps", "what are the challenges"],
    "pdm": ["what should I check daily", "predictive maintenance questions for {x}", "what to inspect on {x}", "early warning signs for {x}",
            "how do I know {x} is fouling", "maintenance tips for {x}", "what wears out on {x}", "monitoring for {x}", "what could fail soon",
            "how to predict clinker", "signs of tar fouling", "shift checks", "simple to tough questions for the operator"],
    "hazop": ["hazop for {x}", "no flow in {x}", "reverse flow into {x}", "more flow through {x}", "loss of cooling water", "what if the engine trips",
              "deviation high temperature in {x}", "what protects against backflow", "what if oxygen gets into the syngas", "guideword less flow on {x}",
              "safeguards for {x}", "what if pressure rises in {x}"],
    "suggest": ["what does the ai model suggest for {x}", "suggest instruments for {x}", "is anything missing on {x}", "model prediction for {x}",
                "what valves should {x} have", "recommend instruments for {x}"],
    "remark": ["add remark on {x}: check nozzle size", "remark {x} confirm set pressure", "note on {x} that the size is too small", "flag {x} for review",
               "raise a comment on {x}", "review comment for {x}"],
    "propose": ["change size of {x} to 8 inch", "propose fail open for {x}", "add a temperature indicator on {x}", "make {x} fail locked",
                "propose correction on {x}", "increase {x} to 12 inch", "add isolation valve on {x}"],
    "count": ["how many control valves", "how many instruments in area 400", "count the shutdown valves", "number of lines", "how many trips",
              "how many equipment items", "total number of valves"],
    "knowledge": ["why use SS310S", "what is metal dusting", "explain tar cracking", "what is a two stage downdraft gasifier", "why is there a flame arrestor",
                  "what does lambda mean for the engine", "what is the boudouard reaction", "why nitrogen purge", "what limits does the engine have on tar",
                  "what is a knock out drum for", "what is cold gas efficiency", "how does a cyclone work", "papers on syngas cleaning"],
}


KEYWORDS = {   # strong cue words: added to the classifier score so rare phrasings still land on the right tool
    "fail_position": ["fail position", "fail open", "fail closed", "fail safe", "fail locked", " fc", " fo ", "air failure", "actuator"],
    "setpoint": ["set point", "setpoint", "alarm", "trip setting", "trip at", "trips at", "range of", "normal value", "high high", "low low"],
    "nozzles": ["nozzle", "manway", "drain", "vent", "connection size"],
    "moc": ["material", "moc", "steel", "ss316", "ss310", "pipe class", "insulat", "thread", "gasket", "design pressure", "design temperature",
            "corrosion", "test pressure", "spec break", "end connection", "metallurgy"],
    "failure": ["what if", "fails", "failure", "sticks", "stuck", "breaks", "goes wrong", "spurious", "fmea"],
    "hazop": ["hazop", "deviation", "no flow", "reverse flow", "back flow", "backflow", "loss of", "more flow", "less flow", "safeguard", "guideword"],
    "balance": ["efficien", "balance", "heat loss", "lhv", "net power", "conversion", "how much", "closure", "kw", "yield"],
    "pdm": ["daily", "shift", "inspect", "predictive", "maintenance", "fouling", "wear", "early warning", "monitor", "operator"],
    "path": ["path", "route", "between"],
    "neighbours": ["downstream", "upstream", "connected to", "around", "neighbour", "feeds"],
    "count": ["how many", "count", "number of", "total number"],
    "bom": ["bom", "bill of material", "take off", "cost", "buy", "quantit"],
    "check": ["missing", "open remark", "pending", "status", "gaps", "challenges", "disagree"],
    "knowledge": ["why", "explain", "what is a", "how does", "paper", "meaning", "define"],
    "remark": ["remark", "comment", "flag "],
    "propose": ["change ", "propose", "increase", "make it", "add a", "add an"],
    "suggest": ["suggest", "recommend", "ai model", "prediction"],
}
SYN = [("fails", ["trips", "stops", "breaks down", "is lost", "malfunctions"]), ("material", ["metallurgy", "steel grade", "moc"]),
       ("what is", ["tell me", "show me", "please give", "i want to know"]), ("check", ["review", "verify"]), ("how many", ["count", "number of"])]
PREFIX = ["", "", "please ", "can you tell me ", "quick question: ", "i need to know ", "copilot, "]


def keyword_scores(text, intents):
    t = " " + text.lower() + " "
    return np.array([sum(1 for k in KEYWORDS.get(i, []) if k in t) for i in intents], dtype=float)


def _features(text):
    t = re.sub(r"[A-Z]{1,5}-\d{3,4}[A-Z]?|\d+(?:\.\d+)?\"?-[A-Z]{2,4}-\d{3,4}-[A-Z0-9]+-[A-Z]", " TAG ", text)
    w = re.findall(r"[a-z0-9]+", t.lower())
    feats = (w + [a + "_" + b for a, b in zip(w, w[1:])] + ["^" + w[0]] + ["#" + x[i:i + 4] for x in w if len(x) > 4 for i in range(len(x) - 3)]) if w else []
    idx = [zlib.crc32(f.encode()) % DIM for f in feats]
    v = np.zeros(DIM, dtype=np.float32)
    for i in idx:
        v[i] += 1
    n = np.linalg.norm(v)
    return v / n if n else v


def _fill(t, tags, rng):
    t = rng.choice(PREFIX) + t
    for a, alts in SYN:
        if a in t and rng.random() < 0.4:
            t = t.replace(a, rng.choice(alts), 1)
    return t.replace("{x}", rng.choice(tags)).replace("{y}", rng.choice(tags))


def training_set(tags, extra=None, seed=0, held_out=0.2):
    rng = random.Random(seed)
    train, test = [], []
    for intent, temps in TEMPLATES.items():
        temps = list(temps)
        rng.shuffle(temps)
        k = max(1, int(len(temps) * held_out))
        for i, t in enumerate(temps):
            dest = test if i < k else train
            for _ in range(8 if "{x}" in t else 5):
                dest.append((_fill(t, tags, rng), intent))
    if extra is not None:
        for r in extra.itertuples():
            for _ in range(4):
                train.append((r.text, r.intent))
    return train, test


class IntentModel:
    def __init__(self, intents=None, W=None, b=None, card=None):
        self.intents = intents or sorted(TEMPLATES)
        self.W, self.b, self.card = W, b, card or {}

    def fit(self, data, epochs=60, lr=0.5, l2=1e-4, seed=0):
        X = np.stack([_features(t) for t, _ in data])
        y = np.array([self.intents.index(i) for _, i in data])
        rng = np.random.default_rng(seed)
        K = len(self.intents)
        self.W = np.zeros((DIM, K), dtype=np.float32)
        self.b = np.zeros(K, dtype=np.float32)
        for _ in range(epochs):
            order = rng.permutation(len(y))
            for s in range(0, len(y), 64):
                j = order[s:s + 64]
                Z = X[j] @ self.W + self.b
                Z -= Z.max(1, keepdims=True)
                P = np.exp(Z)
                P /= P.sum(1, keepdims=True)
                P[np.arange(len(j)), y[j]] -= 1
                self.W -= lr * (X[j].T @ P / len(j) + l2 * self.W)
                self.b -= lr * P.mean(0)
        return self

    def predict(self, text, k=3):
        z = _features(text) @ self.W + self.b + 1.5 * keyword_scores(text, self.intents)
        p = np.exp(z - z.max())
        p /= p.sum()
        o = np.argsort(-p)[:k]
        return [(self.intents[i], float(p[i])) for i in o]

    def accuracy(self, data):
        return float(np.mean([self.predict(t, 1)[0][0] == i for t, i in data])) if data else None

    def save(self, path=MODEL):
        path.parent.mkdir(exist_ok=True)
        np.savez_compressed(path, W=self.W.astype(np.float16), b=self.b)
        path.with_suffix(".json").write_text(json.dumps(dict(intents=self.intents, card=self.card), indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path=MODEL):
        z = np.load(path)
        meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        return cls(meta["intents"], z["W"].astype(np.float32), z["b"], meta.get("card"))


def examples():
    return pd.read_csv(EXAMPLES) if EXAMPLES.exists() else pd.DataFrame(columns=["text", "intent"])


def add_example(text, intent):
    df = examples()
    df.loc[len(df)] = [text, intent]
    df.to_csv(EXAMPLES, index=False)


def train(tags, save=True):
    extra = examples()
    tr, te = training_set(tags, extra)
    m = IntentModel().fit(tr)
    m.card = dict(train_examples=len(tr), held_out_examples=len(te), user_examples=len(extra),
                  held_out_accuracy=round(m.accuracy(te), 3), train_accuracy=round(m.accuracy(tr), 3),
                  note="held-out = question templates never used in training, filled with plant tags")
    if save:
        m.save()
    return m


def load_or_train(tags):
    try:
        return IntentModel.load()
    except Exception:
        return train(tags)
