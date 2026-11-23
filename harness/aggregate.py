"""Aggregate raw per-run outputs into the CSV deliverables."""

import csv
import glob
import json
import os
import re
from collections import Counter


def load_suite(raw):
    runs = {}
    for p in glob.glob(os.path.join(raw, "suite_run_*.json")):
        k = int(re.search(r"suite_run_(\d+)\.json", p).group(1))
        runs[k] = json.load(open(p))
    return runs


def load_iso(raw):
    res, errs, times = {}, [], {}
    for p in sorted(glob.glob(os.path.join(raw, "iso_shard*.jsonl"))):
        for line in open(p):
            try:
                d = json.loads(line)
            except Exception:
                continue
            res[(d["id"], d["rep"])] = d["o"]
            times[(d["id"], d["rep"])] = d["d"]
            if d["o"] in ("0", "N") and d.get("err"):
                errs.append(d)
    return res, errs, times


def stats(row):
    c = Counter(row)
    p, f, s, n = c["1"], c["0"], c["S"], c["N"]
    executed = p + f
    return p, f, s, n, (f / executed if executed else 0.0)


def classify(p, f, s, n):
    if p and f:
        return "flaky"
    if f and not p:
        return "always_fail"
    if p and not f:
        return "always_pass"
    if s and not n:
        return "always_skipped"
    return "no_result"


def write_setting(outdir, name, tests, rows, nruns, pad_to):
    cols = max(nruns, pad_to)
    with open(os.path.join(outdir, f"{name}_results.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["test_id", "test_name", "module", "sha"] + [f"run_{i}" for i in range(1, cols + 1)])
        for t in tests:
            w.writerow([t["test_id"], t["test_name"], t["module"], t["sha"]] + rows[t["test_id"]] + ["N"] * (cols - nruns))
    with open(os.path.join(outdir, f"{name}_failure_summary.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["test_name", "fail_runs_count", "first_failing_run", "all_failing_runs"])
        for t in tests:
            fails = [i + 1 for i, v in enumerate(rows[t["test_id"]]) if v == "0"]
            w.writerow([t["test_id"], len(fails), fails[0] if fails else "", ";".join(map(str, fails))])
    with open(os.path.join(outdir, f"{name}_stats.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["test_id", "executed_runs", "pass", "fail", "skipped", "missing", "failure_rate", "class"])
        for t in tests:
            p, fl, s, n, fr = stats(rows[t["test_id"]])
            w.writerow([t["test_id"], nruns, p, fl, s, n, f"{fr:.6f}", classify(p, fl, s, n)])


def main(project, outdir, tests_csv, pad_to):
    raw = os.path.join(outdir, "raw")
    tests = list(csv.DictReader(open(tests_csv)))
    ids = [t["test_id"] for t in tests]
    summary = {}

    suite = load_suite(raw)
    srows = None
    if suite:
        ks = sorted(suite)
        assert ks == list(range(1, len(ks) + 1)), "suite runs must be contiguous from 1"
        srows = {i: [suite[k].get(i, ["N"])[0] for k in ks] for i in ids}
        write_setting(outdir, "suite", tests, srows, len(ks), pad_to)
        summary["suite_runs"] = len(ks)

    iso, errs, _ = load_iso(raw)
    irows = None
    if iso:
        reps = sorted({r for _, r in iso})
        n = max(reps)
        irows = {i: [iso.get((i, r), "N") for r in range(1, n + 1)] for i in ids}
        write_setting(outdir, "isolation", tests, irows, n, pad_to)
        summary["isolation_reps"] = n
        with open(os.path.join(outdir, "isolation_failure_details.jsonl"), "w") as f:
            for d in errs:
                f.write(json.dumps({"test_id": d["id"], "run": d["rep"], "outcome": d["o"], "error": d["err"]}) + "\n")

    if srows and irows:
        cmp_rows = []
        for t in ids:
            a = classify(*stats(irows[t])[:4])
            b = classify(*stats(srows[t])[:4])
            if a == "flaky" and b == "flaky":
                cat = "flaky_in_both"
            elif a == "flaky":
                cat = "flaky_only_isolation"
            elif b == "flaky":
                cat = "flaky_only_suite"
            elif a == b:
                cat = "consistent_" + a
            else:
                cat = f"not_flaky_(iso={a},suite={b})"
            cmp_rows.append((t, a, b, cat))
        with open(os.path.join(outdir, "comparison.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["test_id", "isolation_class", "suite_class", "category"])
            w.writerows(cmp_rows)
        summary["comparison"] = dict(Counter(c[3] for c in cmp_rows))
    summary["tests"] = len(ids)
    json.dump(summary, open(os.path.join(outdir, "summary.json"), "w"), indent=2)
    print(json.dumps(summary, indent=2))
