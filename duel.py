#!/usr/bin/env python3
import argparse
import concurrent.futures
import csv
import datetime
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MSG_DEFAULT = HERE / "message_config.txt"  # arena is standalone: message config lives next to this script
CONFIG_DIR = HERE / "configs"
RESULTS_DIR = HERE / "results"
RESULT_RE = re.compile(r"state=(\S+) winner=(\S+) turns=(\d+)")
VERSION_RE = re.compile(r"v(\d+(?:\.\d+)*)")


def bot_version(bot):
    """Version read from a bot's filename, e.g. archmage_v1.2 -> (1, 2). Unversioned/foreign names -> () (lowest)."""
    m = VERSION_RE.search(Path(bot).name)
    return tuple(int(p) for p in m.group(1).split(".")) if m else ()


def _reader(proc, q):
    for line in iter(proc.stderr.readline, ""):
        q.put(line.rstrip("\n"))
    q.put(None)  # EOF sentinel


def _pull(q, prefixes, timeout=120.0):
    """Pull lines until one starts with a wanted prefix; returns (prefix, rest) or (None, None) on timeout/EOF."""
    end = time.time() + timeout
    while True:
        try:
            line = q.get(timeout=max(0.01, end - time.time()))
        except queue.Empty:
            return None, None
        if line is None:
            return "EOF", ""
        for p in prefixes:
            if line.startswith(p):
                return p, line[len(p):].strip()
        # other stderr noise (e.g. [AI] telemetry) is ignored


def run_duel(task):
    white_bot, black_bot, config, game_id, time_white, time_black, archive, white_label, game_timeout, msg = task
    cwd_w, cwd_b = tempfile.mkdtemp(prefix="duel_w_"), tempfile.mkdtemp(prefix="duel_b_")

    def spawn(bot, cwd, t_ms):
        env = dict(os.environ)
        if t_ms:
            env["BOT_TIME_MS"] = str(t_ms)
        return subprocess.Popen([str(bot), str(config), str(msg)],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                cwd=cwd, env=env, text=True, bufsize=1)

    pw, pb = spawn(white_bot, cwd_w, time_white), spawn(black_bot, cwd_b, time_black)
    qw, qb = queue.Queue(), queue.Queue()
    threading.Thread(target=_reader, args=(pw, qw), daemon=True).start()
    threading.Thread(target=_reader, args=(pb, qb), daemon=True).start()
    procs, queues = {"white": pw, "black": pb}, {"white": qw, "black": qb}

    res = {"game_id": game_id, "config": config.name, "state": "NO_RESULT", "winner_color": "none", "turns": 0}
    deadline = time.time() + game_timeout
    try:
        while True:
            if time.time() > deadline:  # guard: a mirror desync can loop forever -> bound each game
                res["state"] = "TIMEOUT"
                break
            kind, val = _pull(qw, ["@PROMPT", "RESULT", "EOF"], timeout=30.0)  # white process is the clock
            if kind in (None, "EOF"):
                break
            if kind == "RESULT":
                m = RESULT_RE.search(val)
                if m:
                    res["state"], res["winner_color"], res["turns"] = m.group(1), m.group(2), int(m.group(3))
                break
            color = val.strip()
            if color not in procs:
                break
            owner, other = procs[color], procs["black" if color == "white" else "white"]
            owner.stdin.write("play\n")
            owner.stdin.flush()
            mk, cmd = _pull(queues[color], ["@MOVE", "RESULT", "EOF"], timeout=30.0)
            if mk == "@MOVE":
                try:
                    other.stdin.write(cmd + "\n")
                    other.stdin.flush()
                except (BrokenPipeError, OSError):
                    break
            elif mk == "RESULT":
                m = RESULT_RE.search(cmd)
                if m:
                    res["state"], res["winner_color"], res["turns"] = m.group(1), m.group(2), int(m.group(3))
                break
            else:
                break
    finally:
        best, best_key = None, None
        for cwd, bot in ((cwd_w, white_bot), (cwd_b, black_bot)):  # white first -> wins full ties via >
            src = Path(cwd) / "engine_state.json"
            try:
                turns = json.loads(src.read_text()).get("turns")
            except (ValueError, OSError):
                continue  # missing, unreadable, or a foreign bot's non-replay file
            if not isinstance(turns, list) or not turns:
                continue
            key = (bot_version(bot), 1 if "command" in turns[0] else 0)
            if best_key is None or key > best_key:
                best, best_key = src, key
        if best is not None:
            shutil.copy(best, archive / f"{game_id}.json")
        for p in (pw, pb):
            try:
                p.kill()
            except Exception:
                pass
        shutil.rmtree(cwd_w, ignore_errors=True)
        shutil.rmtree(cwd_b, ignore_errors=True)

    wc = res["winner_color"]
    res["winner"] = white_label if wc == "white" else ("B" if white_label == "A" else "A") if wc == "black" else "draw"
    return res


def summarize(results, archive, name_a, name_b):
    n = len(results) or 1
    a = sum(1 for r in results if r["winner"] == "A")
    b = sum(1 for r in results if r["winner"] == "B")
    d = sum(1 for r in results if r["winner"] == "draw")
    # decisive win-rate for A (draws excluded) -> the "does new beat old" number
    dec = a + b
    a_wr = 100 * a / dec if dec else 0.0

    # per-colour: how each bot did as white
    a_white = [r for r in results if r["game_id"].endswith("_Aw")]
    b_white = [r for r in results if r["game_id"].endswith("_Bw")]
    aw_wins = sum(1 for r in a_white if r["winner"] == "A")
    bw_wins = sum(1 for r in b_white if r["winner"] == "B")

    states = {}
    for r in results:
        states[r["state"]] = states.get(r["state"], 0) + 1

    lines = [
        f"A = {name_a}",
        f"B = {name_b}",
        f"games: {len(results)}",
        f"A wins:  {a} ({100*a/n:.1f}%)",
        f"B wins:  {b} ({100*b/n:.1f}%)",
        f"draws:   {d} ({100*d/n:.1f}%)",
        f"A decisive win-rate (draws excl.): {a_wr:.1f}%   ({a}-{b})",
        f"A as white: {aw_wins}/{len(a_white)} wins   |   B as white: {bw_wins}/{len(b_white)} wins",
        f"avg turns: {sum(r['turns'] for r in results)/n:.1f}",
        "end states:",
    ]
    lines += [f"  {s}: {c}" for s, c in sorted(states.items(), key=lambda x: -x[1])]
    report = "\n".join(lines)
    print("\n=== DUEL SUMMARY ===\n" + report)
    (archive / "summary.txt").write_text(report + "\n", encoding="utf-8")
    with open(archive / "games.csv", "w", newline="") as f:
        wtr = csv.DictWriter(f, fieldnames=["game_id", "config", "state", "winner_color", "winner", "turns"])
        wtr.writeheader()
        wtr.writerows(results)
    print(f"\narchived per-game JSON + summary.txt + games.csv in {archive}")


def _kill_descendants(root):
    kids = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                line = f.read()
            # format: "pid (comm) state ppid ..."  -- comm may contain spaces/parens, so split after the last ')'
            ppid = int(line[line.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(entry))

    found, stack = [], list(kids.get(root, []))
    while stack:
        pid = stack.pop()
        found.append(pid)
        stack.extend(kids.get(pid, []))
    for pid in found:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return found


def _install_signal_cleanup():
    def handler(signum, _frame):
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # ignore repeats while we tear down
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        sys.stderr.write("\n[duel] interrupted -> killing worker + bot processes ...\n")
        killed = _kill_descendants(os.getpid())
        sys.stderr.write(f"[duel] killed {len(killed)} descendant process(es); exiting\n")
        os._exit(130)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def main():
    _install_signal_cleanup()  # Ctrl+C (and SIGTERM) reliably tears down the pool + bot subprocesses, no orphans
    ap = argparse.ArgumentParser(description="Duel two bot binaries over the arena configs (colours swapped).")
    ap.add_argument("bot_a")
    ap.add_argument("bot_b")
    ap.add_argument("--jobs", type=int, default=5)
    ap.add_argument("--time-ms", type=int, default=0, help="per-move budget for both bots (0 = bot default 900)")
    ap.add_argument("--time-a-ms", type=int, default=None, help="override budget for bot A only")
    ap.add_argument("--time-b-ms", type=int, default=None, help="override budget for bot B only")
    ap.add_argument("--configs", default=str(CONFIG_DIR))
    ap.add_argument("--message-config", default=str(MSG_DEFAULT),
                    help="path to the bots' message_config.txt (default: next to duel.py)")
    ap.add_argument("--limit", type=int, default=0, help="only the first N configs (smoke test)")
    ap.add_argument("--game-timeout", type=int, default=300, help="hard per-game wall-clock cap in seconds")
    args = ap.parse_args()

    msg = Path(args.message_config).resolve()
    if not msg.is_file():
        sys.exit(f"message config not found: {msg}\n"
                 f"pass --message-config <path> or place message_config.txt next to duel.py")

    bot_a, bot_b = Path(args.bot_a).resolve(), Path(args.bot_b).resolve()
    configs = sorted(Path(args.configs).glob("*.txt"))
    if args.limit:
        configs = configs[: args.limit]
    date_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = RESULTS_DIR / f"{bot_a.name}_vs_{bot_b.name}_{date_str}"
    archive.mkdir(parents=True, exist_ok=True)

    ta = args.time_a_ms if args.time_a_ms is not None else args.time_ms
    tb = args.time_b_ms if args.time_b_ms is not None else args.time_ms
    tasks = []
    for c in configs:
        # game 1: A white / B black ; game 2: B white / A black  (colour swap for fairness)
        tasks.append((bot_a, bot_b, c, f"{c.stem}_Aw", ta, tb, archive, "A", args.game_timeout, msg))
        tasks.append((bot_b, bot_a, c, f"{c.stem}_Bw", tb, ta, archive, "B", args.game_timeout, msg))

    budget = f"A={ta or 'default'} B={tb or 'default'}"
    print(f"duel {bot_a.name} (A) vs {bot_b.name} (B): {len(tasks)} games, {args.jobs} parallel, time={budget}ms ...")
    results = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as ex:
        for i, r in enumerate(ex.map(run_duel, tasks), 1):
            results.append(r)
            print(f"  [{i}/{len(tasks)}] {r['game_id']}: {r['state']} -> {r['winner']} ({r['turns']}t)")

    summarize(results, archive, bot_a.name, bot_b.name)


if __name__ == "__main__":
    main()
