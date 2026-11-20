"""
workflow.py - corrections, review remarks and revision log for each case (the engineering review workflow).

Files, one set per case (stem = streams file name without "streams_" and ".csv"):
  data/corrections_<stem>.csv   changes to the generated P&ID, applied every time it is regenerated
  data/reviews_<stem>.csv       review remarks against an asset or line, with status and response
  data/revisions_<stem>.csv     log of every change (who, when, what), drives the revision letter on the sheet

Correction actions
  line_items             target = stream no.        value = in-line items, e.g. "ISO;XV;CHK"  (replaces the stream's pid_hints)
  equipment_instruments  target = equipment tag     value = instrument letters, e.g. "LIC;LSHH;TI" (replaces the rule set)
  line_size              target = stream no.        value = size, e.g. 8"                  (replaces the calculated size)
  fail_position          target = valve tag         value = FC, FO or FL                   (replaces the assumed fail position)
Only rows with status "applied" change the drawing; "proposed" rows wait for approval (the copilot proposes, people apply).
"""
import datetime
from pathlib import Path

import pandas as pd

DATA = Path(__file__).parent / "data"
ACTIONS = {"line_items": "stream no.", "equipment_instruments": "equipment tag", "line_size": "stream no.", "fail_position": "valve tag"}
CORR_COLS = ["id", "date", "by", "action", "target", "value", "reason", "status"]
REV_COLS = ["id", "date", "reviewer", "target", "category", "remark", "status", "response", "closed_by", "closed_date"]
LOG_COLS = ["date", "by", "what", "detail"]
CATEGORIES = ["process", "instrumentation", "piping", "safety", "drafting", "other"]


def stem_of(case):
    return Path(case).stem.replace("streams_", "")


def _path(kind, case):
    return DATA / f"{kind}_{stem_of(case)}.csv"


def _load(kind, case, cols):
    f = _path(kind, case)
    if f.exists():
        df = pd.read_csv(f, dtype=str).fillna("")
        for c in cols:
            if c not in df:
                df[c] = ""
        return df[cols]
    return pd.DataFrame(columns=cols)


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def corrections(case):
    return _load("corrections", case, CORR_COLS)


def reviews(case):
    return _load("reviews", case, REV_COLS)


def revisions(case):
    return _load("revisions", case, LOG_COLS)


def log(case, by, what, detail):
    df = revisions(case)
    df.loc[len(df)] = [now(), by or "-", what, detail]
    df.to_csv(_path("revisions", case), index=False)


def revision_letter(case):
    n = len(revisions(case))
    return "A" if n == 0 else f"A.{n}"           # A = draft for review; .n = number of recorded changes


def next_id(df, prefix):
    nums = [int(x.split("-")[1]) for x in df["id"] if isinstance(x, str) and x.startswith(prefix + "-") and x.split("-")[1].isdigit()]
    return f"{prefix}-{(max(nums) + 1) if nums else 1:03d}"


def add_correction(case, action, target, value, reason, by, status="applied"):
    if action not in ACTIONS:
        raise ValueError(f"Unknown action {action}. Use one of: {', '.join(ACTIONS)}")
    df = corrections(case)
    cid = next_id(df, "C")
    df.loc[len(df)] = [cid, now(), by or "-", action, str(target), str(value), reason, status]
    df.to_csv(_path("corrections", case), index=False)
    log(case, by, f"correction {cid} {status}", f"{action} {target} = {value} ({reason})")
    return cid


def set_correction_status(case, cid, status, by):
    df = corrections(case)
    df.loc[df.id == cid, "status"] = status
    df.to_csv(_path("corrections", case), index=False)
    log(case, by, f"correction {cid} {status}", "")


def save_corrections(case, df, by):
    old = corrections(case)
    df = df.fillna("").astype(str)
    for i in df.index:
        if not df.at[i, "id"]:
            df.at[i, "id"] = next_id(df, "C")
            df.at[i, "date"] = df.at[i, "date"] or now()
    df[CORR_COLS].to_csv(_path("corrections", case), index=False)
    if not old.equals(df[CORR_COLS]):
        log(case, by, "corrections edited", f"{len(df)} correction rows")


def add_review(case, target, remark, reviewer, category="other"):
    df = reviews(case)
    rid = next_id(df, "R")
    df.loc[len(df)] = [rid, now(), reviewer or "-", target, category, remark, "open", "", "", ""]
    df.to_csv(_path("reviews", case), index=False)
    log(case, reviewer, f"remark {rid} raised", f"{target}: {remark}")
    return rid


def close_review(case, rid, response, by, status="closed"):
    df = reviews(case)
    m = df.id == rid
    df.loc[m, ["status", "response", "closed_by", "closed_date"]] = [status, response, by or "-", now()]
    df.to_csv(_path("reviews", case), index=False)
    log(case, by, f"remark {rid} {status}", response)


def save_reviews(case, df, by):
    old = reviews(case)
    df = df.fillna("").astype(str)
    for i in df.index:
        if not df.at[i, "id"]:
            df.at[i, "id"] = next_id(df, "R")
            df.at[i, "date"] = df.at[i, "date"] or now()
            df.at[i, "status"] = df.at[i, "status"] or "open"
    df[REV_COLS].to_csv(_path("reviews", case), index=False)
    if not old.equals(df[REV_COLS]):
        log(case, by, "remarks edited", f"{len(df)} remark rows")


def apply(case, eq, st):
    """Apply the 'applied' corrections to the PFD tables. Returns (eq, st, info) where info carries what came from where."""
    df = corrections(case)
    df = df[df.status.str.lower() == "applied"]
    eq, st = eq.copy(), st.copy()
    st["no"] = st["no"].astype(str)
    info = {"fail_override": {}, "hint_source": {}, "inst_source": {}, "size_source": {}, "applied": list(df.id)}
    if "size_override" not in st:
        st["size_override"] = ""
    if "instruments" not in eq:
        eq["instruments"] = ""
    for r in df.itertuples():
        if r.action == "line_items" and (st["no"] == r.target).any():
            st.loc[st["no"] == r.target, "pid_hints"] = r.value
            info["hint_source"][r.target] = r.id
        elif r.action == "equipment_instruments" and (eq.tag == r.target).any():
            eq.loc[eq.tag == r.target, "instruments"] = r.value
            info["inst_source"][r.target] = r.id
        elif r.action == "line_size" and (st["no"] == r.target).any():
            st.loc[st["no"] == r.target, "size_override"] = r.value if r.value.endswith('"') or not r.value else r.value + '"'
            info["size_source"][r.target] = r.id
        elif r.action == "fail_position":
            info["fail_override"][r.target] = (r.value.upper(), r.id)
    return eq, st, info
