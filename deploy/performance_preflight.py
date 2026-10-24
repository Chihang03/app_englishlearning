"""Compare two native releases on server-local snapshots; emit aggregates only."""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request(port, token, path, body=None):
    headers = {"Cookie": "cvt_session=" + token, "Content-Type": "application/json"}
    req = Request(f"http://127.0.0.1:{port}{path}", headers=headers,
                  data=None if body is None else json.dumps(body).encode())
    start = time.perf_counter()
    try:
        response = urlopen(req, timeout=40)
    except HTTPError as error:
        response = error
    with response:
        value = json.load(response)
        return response.status, value, (time.perf_counter() - start) * 1000


def canonical(value):
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in value.items() if k != "attempt_id"}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    return value


def memory(pid):
    return {line.split(":")[0]: int(line.split()[1])
            for line in Path(f"/proc/{pid}/status").read_text().splitlines()
            if line.startswith(("VmRSS:", "VmHWM:"))}


def distribution(samples):
    times = sorted(sample[2] for sample in samples)
    return {"requests": len(times), "statuses": dict(Counter(s[0] for s in samples)),
            "p50_ms": times[(len(times) - 1) // 2], "p95_ms": times[int((len(times) - 1) * .95)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["database", "baseline", "candidate", "directory"]:
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    args.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    snapshot = args.directory / "snapshot.db"
    with closing(sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(snapshot)) as target:
        source.backup(target)
    with closing(sqlite3.connect(snapshot)) as conn:
        user = conn.execute("SELECT u.id FROM users u LEFT JOIN review_history r ON r.user_id=u.id WHERE u.role='learner' GROUP BY u.id ORDER BY COUNT(r.id) DESC,u.id LIMIT 1").fetchone()[0]
    clock = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    token = secrets.token_urlsafe(32)
    outputs, results = [], []
    for name, binary in [("baseline", args.baseline), ("candidate", args.candidate)]:
        database = args.directory / (name + ".db")
        with closing(sqlite3.connect(snapshot)) as source, closing(sqlite3.connect(database)) as target:
            source.backup(target)
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                         (hashlib.sha256(token.encode()).hexdigest(), user, clock, "2099-01-01T00:00:00+00:00"))
            conn.commit()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        with (args.directory / (name + ".log")).open("wb") as log:
            launch_started = time.perf_counter()
            child = subprocess.Popen([str(binary), "--database", str(database), "--listen", f"127.0.0.1:{port}", "--enable-api", "--test-clock", clock], stdout=log, stderr=log, env={**os.environ, "RUST_LOG": "warn"})
            try:
                for _ in range(1500):
                    try:
                        if request(port, token, "/api/health")[0] == 200:
                            break
                    except OSError:
                        pass
                    assert child.poll() is None, name + " exited; inspect local log"
                    time.sleep(.02)
                else:
                    raise RuntimeError(name + " readiness timed out")
                record, output = {"kind": name, "startup_seconds": time.perf_counter() - launch_started, "endpoints": {}}, []
                paths = ["/api/stats", "/api/word-lists", "/api/settings", "/api/learning-calendar", "/api/dictionary/address"]
                for path in paths:
                    cold = request(port, token, path)
                    assert cold[0] == 200, (name, path, cold[0], cold[1])
                    output.append(canonical(cold[1]))
                    samples = [request(port, token, path) for _ in range(10)]
                    assert all(s[0] == 200 for s in samples), (name, path, "warm failure")
                    record["endpoints"][path] = {"first_ms": cold[2], **distribution(samples)}
                record["mixed"] = []
                for concurrency in [1, 5, 20]:
                    start = time.perf_counter()
                    with ThreadPoolExecutor(max_workers=concurrency) as pool:
                        samples = list(pool.map(lambda i: request(port, token, paths[i % len(paths)]), range(60)))
                    record["mixed"].append({"concurrency": concurrency, "elapsed_seconds": time.perf_counter() - start, **distribution(samples), **memory(child.pid)})
                # Exercise real writes only on each isolated copy.
                status, payload, elapsed = request(port, token, "/api/next")
                assert status == 200, (name, "next", status)
                record["next_ms"] = elapsed
                output.append(canonical(payload))
                card = payload["card"]
                if card:
                    for cached in [False, True]:
                        reply = request(port, token, "/api/study/meanings", {"attempt_id": card["attempt_id"], "content_cached": cached})
                        assert reply[0] == 200, (name, "meanings", reply[0])
                        output.append(canonical(reply[1]))
                        record["meanings_cached_ms" if cached else "meanings_ms"] = reply[2]
                        samples = [request(port, token, "/api/study/meanings", {"attempt_id": card["attempt_id"], "content_cached": cached}) for _ in range(10)]
                        assert all(s[0] == 200 for s in samples)
                        record["meanings_cached_warm" if cached else "meanings_warm"] = distribution(samples)
                    review = {"word_id": card["id"], "sense_id": card["sense_id"], "example_id": card["example_id"], "learning_unit_id": card["learning_unit_id"], "attempt_id": card["attempt_id"], "user_answer": card["answer_form"], "active_response_ms": 1234}
                    reply = request(port, token, "/api/review", review)
                    assert reply[0] == 200, (name, "review", reply[0])
                    output.append(canonical(reply[1]))
                    for path in ["/api/stats", "/api/word-lists", "/api/learning-calendar"]:
                        reply = request(port, token, path)
                        assert reply[0] == 200
                        output.append(canonical(reply[1]))
                    reply = request(port, token, "/api/next")
                    assert reply[0] == 200
                    output.append(canonical(reply[1]))
                    record["after_review_next_ms"] = reply[2]
                # Hash a streamed catalogue without loading 35 MB of JSON.
                req = Request(f"http://127.0.0.1:{port}/api/study/meaning-catalog", headers={"Cookie": "cvt_session=" + token})
                with urlopen(req, timeout=40) as response:
                    digest, size = hashlib.sha256(), 0
                    while chunk := response.read(65536):
                        size += len(chunk); digest.update(chunk)
                record["catalogue"] = {"bytes": size, "sha256": digest.hexdigest()}
                with closing(sqlite3.connect(database)) as conn:
                    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                    assert not conn.execute("PRAGMA foreign_key_check").fetchall()
                    record["learning_counts"] = {table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in ["review_history", "sense_srs_state", "adaptive_memory", "sense_exposures"]}
                record.update(memory(child.pid))
                (args.directory / (name + "-result.json")).write_text(json.dumps(record, indent=2) + "\n")
                outputs.append(output); results.append(record)
            finally:
                child.terminate()
                try:
                    child.wait(timeout=40)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait()
    assert outputs[0] == outputs[1], "response parity mismatch; inspect snapshots locally"
    assert results[0]["catalogue"] == results[1]["catalogue"], "catalogue changed"
    assert results[0]["learning_counts"] == results[1]["learning_counts"], "learning counts changed"
    result = {"clock": clock, "snapshot_only": True, "response_parity": True, "results": results}
    (args.directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
