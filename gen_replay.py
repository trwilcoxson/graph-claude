#!/usr/bin/env python3
"""Bake a finished workflow's planned->running->done lifecycle into a
self-contained replay page (artifact.html) that animates the graph AND lets you
click any node to read its real transcript — no server needed. Everything is
reconstructed from the actual run's transcripts and timings; nothing is mocked.

    python3 gen_replay.py <run_dir | runid> [out.html]
"""
import copy, glob, json, os, sys
import wfviz


def frame_at(G, T):
    f = copy.deepcopy(G)
    done = run = 0
    tot = 0
    for n in f["nodes"]:
        s, d = n.get("startedAt"), n.get("durationMs")
        if not s or T < s:
            n["state"] = "planned"; n["tokens"] = 0; n["durationMs"] = None; n["resultPreview"] = ""
        elif d and T < s + d:
            n["state"] = "running"; n["tokens"] = int(n["tokens"] * max(0.05, (T - s) / d))
            n["durationMs"] = int(T - s); n["resultPreview"] = ""; run += 1
        else:
            n["state"] = "done"; done += 1
        tot += n["tokens"]
    started = [n["startedAt"] for n in f["nodes"] if n.get("startedAt") and T >= n["startedAt"]]
    planned = sum(1 for n in f["nodes"] if n["state"] == "planned")
    f["status"] = "planned" if planned == len(f["nodes"]) else ("done" if run == 0 and planned == 0 else "running")
    f["totals"] = {**G["totals"], "done": done, "running": run, "queued": 0, "planned": planned,
                   "tokens": tot, "elapsedMs": int(T - min(started)) if started else 0}
    return f


def resolve(arg):
    if os.path.isdir(arg):
        return arg
    hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/*/subagents/workflows/*{arg}*"))
    hits = [h for h in hits if os.path.isdir(h)]
    if not hits:
        sys.exit(f"no run dir found for {arg!r}")
    return hits[0]


def main():
    run_dir = resolve(sys.argv[1])
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), "artifact.html")
    G = wfviz.build_graph(run_dir)

    starts = [n["startedAt"] for n in G["nodes"] if n.get("startedAt")]
    ends = [n["startedAt"] + (n["durationMs"] or 0) for n in G["nodes"] if n.get("startedAt")]
    t0, t1 = min(starts), max(ends)
    span = max(1, t1 - t0)
    N = 18
    times = [t0 - 400] + [t0 + span * i / (N - 1) for i in range(N)] + [t1 + 500]
    frames = [{"state": frame_at(G, T)} for T in times]

    transcripts = {}
    for n in G["nodes"]:
        transcripts[n["agentId"]] = wfviz.build_agent(run_dir, n["agentId"])

    replay = {"name": G["name"], "run": G["run"], "intervalMs": 820,
              "frames": frames, "transcripts": transcripts}

    html = open(os.path.join(os.path.dirname(__file__), "dashboard.html")).read()
    html = "\n".join(l for l in html.splitlines() if 'rel="icon"' not in l)
    inject = "<script>window.__WFVIZ_REPLAY__=" + json.dumps(replay) + ";</script>\n<script>"
    html = html.replace("<script>", inject, 1)
    open(out, "w").write(html)
    print(f"wrote {out}  ({len(frames)} frames, {len(transcripts)} transcripts, {len(html)//1024}kb)")


if __name__ == "__main__":
    main()
