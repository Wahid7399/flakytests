#!/usr/bin/env python3
"""Driver for the flaky-test study.  See README.md for the full workflow.

  study.py collect   <project>
  study.py suite     <project> --runs N [--start K]
  study.py isolation <project> --reps N [--shard k/n] [--limit-tests M]
  study.py aggregate <project> [--pad-to 5000]
"""

import argparse
import csv
import glob
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "harness"

PROJECTS = {
    "openai-agents-python": {
        "cwd": ROOT / "projects/openai-agents-python",
        "sha": "e23c6ffb0fe40cbae3c8d832940d9459571e75da",
        "args": ["tests"],
    },
    "semantic-kernel": {
        "cwd": ROOT / "projects/semantic-kernel/python",
        "sha": "58ad1a3780159300c766477f258a4cdfa768eedf",
        "args": ["tests/unit", "--ignore=tests/unit/processes/dapr_runtime"],
    },
}


def pytest_cmd(p, extra):
    cfg = PROJECTS[p]
    return (
        [str(cfg["cwd"] / ".venv/bin/python"), "-m", "pytest", "-p", "flaky_plugin", "-p", "no:cacheprovider", "-q"]
        + cfg["args"]
        + extra
    )


def env():
    e = dict(os.environ)
    e["PYTHONPATH"] = str(HARNESS) + os.pathsep + e.get("PYTHONPATH", "")
    e["PYTHONHASHSEED"] = e.get("PYTHONHASHSEED", "0")
    # The sandbox injects GIT_CONFIG_*=safe.bareRepository=explicit, which breaks tests that use bare repos.
    if e.get("GIT_CONFIG_KEY_0") == "safe.bareRepository":
        e["GIT_CONFIG_VALUE_0"] = "all"
    return e


def res_dir(p):
    d = ROOT / "results" / p
    (d / 'logs').mkdir(parents=True, exist_ok=True)
    return d


def tests_csv(p):
    return res_dir(p) / "tests.csv"


def cmd_collect(a):
    out = res_dir(a.project) / "raw"
    cmd = pytest_cmd(a.project, ["--collect-only", "--flaky-mode=collect", f"--flaky-out={out}"])
    r = subprocess.run(cmd, cwd=PROJECTS[a.project]["cwd"], env=env(), capture_output=True, text=True)
    (res_dir(a.project) / "collect.log").write_text(r.stdout + r.stderr)
    items = json.load(open(out / "collected.json"))
    ids = [i[0] for i in items]
    assert len(ids) == len(set(ids)), "duplicate test ids"
    with open(tests_csv(a.project), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["test_id", "test_name", "module", "sha"])
        for nodeid, name, module in items:
            short = nodeid.split("::", 1)[1] if "::" in nodeid else nodeid
            w.writerow([nodeid, short, nodeid.split("::", 1)[0], PROJECTS[a.project]["sha"]])
    print(f"{a.project}: {len(ids)} tests collected")


def cmd_suite(a):
    out = res_dir(a.project) / "raw"
    logs = res_dir(a.project) / "logs"
    logs.mkdir(exist_ok=True)
    meta = open(res_dir(a.project) / "suite_runs.jsonl", "a")
    for k in range(a.start, a.start + a.runs):
        if (out / f"suite_run_{k}.json").exists():
            continue
        t0 = time.time()
        cmd = pytest_cmd(a.project, ["--flaky-mode=suite", f"--flaky-out={out}", f"--flaky-run-id={k}"])
        with open(logs / f"suite_run_{k}.log", "w") as lf:
            r = subprocess.run(cmd, cwd=PROJECTS[a.project]["cwd"], env=env(), stdout=lf, stderr=subprocess.STDOUT)
        meta.write(json.dumps({"run": k, "exit": r.returncode, "wall_s": round(time.time() - t0, 2),
                               "start": t0}) + "\n")
        meta.flush()
        print(f"suite run {k}: exit={r.returncode} {time.time() - t0:.1f}s", flush=True)


def cmd_isolation(a):
    out = res_dir(a.project) / "raw"
    src = tests_csv(a.project)
    if a.limit_tests:
        sub = res_dir(a.project) / f"tests_subset_{a.limit_tests}.csv"
        rows = list(csv.DictReader(open(src)))
        step = max(1, len(rows) // a.limit_tests)
        with open(sub, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows[::step][: a.limit_tests])
        src = sub
    cmd = pytest_cmd(a.project, [
        "-s", "--flaky-mode=isolation", f"--flaky-out={out}", f"--flaky-tests-file={src}",
        f"--flaky-reps={a.reps}", f"--flaky-start-rep={a.start}", f"--flaky-shard={a.shard}",
    ])
    t0 = time.time()
    shard = a.shard.replace("/", "of")
    with open(res_dir(a.project) / "logs" / f"isolation_{shard}.log", "a") as lf:
        r = subprocess.run(cmd, cwd=PROJECTS[a.project]["cwd"], env=env(), stdout=lf, stderr=subprocess.STDOUT)
    print(f"isolation shard {a.shard}: exit={r.returncode} {time.time() - t0:.1f}s")


def cmd_aggregate(a):
    import aggregate
    aggregate.main(a.project, res_dir(a.project), tests_csv(a.project), a.pad_to)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name in ("collect", "suite", "isolation", "aggregate"):
        s = sp.add_parser(name)
        s.add_argument("project", choices=PROJECTS)
        if name == "suite":
            s.add_argument("--runs", type=int, required=True)
            s.add_argument("--start", type=int, default=1)
        if name == "isolation":
            s.add_argument("--reps", type=int, required=True)
            s.add_argument("--start", type=int, default=1)
            s.add_argument("--shard", default="0/1")
            s.add_argument("--limit-tests", type=int, default=0)
        if name == "aggregate":
            s.add_argument("--pad-to", type=int, default=0)
    a = ap.parse_args()
    # Background launches ignore SIGINT, which is inherited by pytest and breaks tests that send SIGINT.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    sys.path.insert(0, str(HARNESS))
    globals()["cmd_" + a.cmd](a)


if __name__ == "__main__":
    main()
