"""pytest plugin used by the flaky-test study.

Modes (``--flaky-mode``):
  collect    write every collected test id to ``--flaky-out/collected.json`` (no tests are run)
  suite      run the normal pytest session once and record one outcome per test
             -> ``--flaky-out/suite_run_<id>.json`` (+ ``.failures.log`` with tracebacks)
  isolation  for every test in ``--flaky-tests-file`` and every repetition, fork a fresh
             process from the already-collected session and run only that test
             -> ``--flaky-out/iso_shard<k>of<n>.jsonl`` (append only, resumable)

Outcome codes: 1 pass, 0 fail/error, S skipped (incl. xfail), N no result (crash/hard timeout).
"""

import csv
import json
import os
import select
import signal
import time
import traceback

MAX_ERR = 20000


def pytest_addoption(parser):
    g = parser.getgroup("flaky")
    g.addoption("--flaky-mode", default=None, choices=["collect", "suite", "isolation"])
    g.addoption("--flaky-out", default="flaky_out")
    g.addoption("--flaky-run-id", default="1")
    g.addoption("--flaky-tests-file", default=None)
    g.addoption("--flaky-reps", type=int, default=1)
    g.addoption("--flaky-start-rep", type=int, default=1)
    g.addoption("--flaky-shard", default="0/1", help="k/n: handle tests whose index %% n == k")
    g.addoption("--flaky-hard-timeout", type=float, default=300.0)


def _mode(config):
    return config.getoption("flaky_mode")


def summarize(reports):
    """Collapse setup/call/teardown reports of one test into (code, duration, error text)."""
    code, dur, err = None, 0.0, ""
    for r in reports:
        dur += getattr(r, "duration", 0.0) or 0.0
        if r.outcome == "failed":
            code = "0"
            err += f"--- {r.when} ---\n{r.longreprtext}\n"
        elif r.outcome == "skipped" and code != "0":
            code = "S"
        elif r.outcome == "passed" and r.when == "call" and code is None:
            code = "1"
    return (code or "N"), dur, err[:MAX_ERR]


class _Collector:
    def __init__(self):
        self.reports = {}

    def pytest_runtest_logreport(self, report):
        self.reports.setdefault(report.nodeid, []).append(report)


def pytest_configure(config):
    if _mode(config):
        config._flaky = _Collector()
        config.pluginmanager.register(config._flaky, "flaky-collector")


def pytest_collection_finish(session):
    if _mode(session.config) == "collect":
        out = session.config.getoption("flaky_out")
        os.makedirs(out, exist_ok=True)
        ids = [(i.nodeid, i.name, i.location[0]) for i in session.items]
        with open(os.path.join(out, "collected.json"), "w") as f:
            json.dump(ids, f)


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    if _mode(config) != "suite":
        return
    out = config.getoption("flaky_out")
    rid = config.getoption("flaky_run_id")
    os.makedirs(out, exist_ok=True)
    res, logs = {}, []
    for nodeid, reps in config._flaky.reports.items():
        code, dur, err = summarize(reps)
        res[nodeid] = [code, round(dur, 6)]
        if code == "0":
            logs.append(f"=== {nodeid} (run {rid}) ===\n{err}")
    with open(os.path.join(out, f"suite_run_{rid}.json"), "w") as f:
        json.dump(res, f)
    if logs:
        with open(os.path.join(out, f"suite_run_{rid}.failures.log"), "w") as f:
            f.write("\n".join(logs))


def _run_child(item, wfd):
    """Executed in the forked child: run exactly one test and ship the result through the pipe."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        item.config._flaky.reports.clear()
        item.config.hook.pytest_runtest_protocol(item=item, nextitem=None)
        code, dur, err = summarize(item.config._flaky.reports.get(item.nodeid, []))
        payload = {"o": code, "d": round(dur, 6), "err": err}
    except BaseException:  # noqa: BLE001
        payload = {"o": "N", "d": 0.0, "err": "harness error in child:\n" + traceback.format_exc()[:MAX_ERR]}
    data = json.dumps(payload).encode()
    while data:
        data = data[os.write(wfd, data):]
    os._exit(0)


def _run_isolated(item, hard_timeout):
    rfd, wfd = os.pipe()
    t0 = time.time()
    pid = os.fork()
    if pid == 0:
        os.close(rfd)
        os.setsid()
        _run_child(item, wfd)
    os.close(wfd)
    buf, timed_out = b"", False
    while True:
        left = hard_timeout - (time.time() - t0)
        if left <= 0:
            timed_out = True
            break
        r, _, _ = select.select([rfd], [], [], left)
        if not r:
            timed_out = True
            break
        chunk = os.read(rfd, 65536)
        if not chunk:
            break
        buf += chunk
    os.close(rfd)
    if timed_out:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _, status = os.waitpid(pid, 0)
    wall = round(time.time() - t0, 6)
    try:
        res = json.loads(buf.decode())
    except Exception:  # noqa: BLE001
        reason = "hard timeout" if timed_out else f"child died without result (wait status {status})"
        res = {"o": "N", "d": wall, "err": reason}
    return res


def pytest_runtestloop(session):
    config = session.config
    mode = _mode(config)
    if mode == "collect":
        return True
    if mode != "isolation":
        return None
    out = config.getoption("flaky_out")
    os.makedirs(out, exist_ok=True)
    k, n = (int(x) for x in config.getoption("flaky_shard").split("/"))
    tf = config.getoption("flaky_tests_file")
    if tf:
        with open(tf, newline="") as f:
            order = [row["test_id"] for row in csv.DictReader(f)]
    else:
        order = [i.nodeid for i in session.items]
    by_id = {i.nodeid: i for i in session.items}
    mine = [t for idx, t in enumerate(order) if idx % n == k]
    reps = config.getoption("flaky_reps")
    start = config.getoption("flaky_start_rep")
    hard = config.getoption("flaky_hard_timeout")
    path = os.path.join(out, f"iso_shard{k}of{n}.jsonl")
    done = set()
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                    done.add((d["id"], d["rep"]))
                except Exception:  # noqa: BLE001
                    pass
    with open(path, "a") as f:
        for tid in mine:
            item = by_id.get(tid)
            for rep in range(start, start + reps):
                if (tid, rep) in done:
                    continue
                if item is None:
                    res = {"o": "N", "d": 0.0, "err": "test not collected"}
                else:
                    res = _run_isolated(item, hard)
                if res["o"] in ("1", "S"):
                    res["err"] = ""
                res.update(id=tid, rep=rep)
                f.write(json.dumps(res) + "\n")
            f.flush()
    return True
