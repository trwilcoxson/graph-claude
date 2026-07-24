#!/usr/bin/env python3
"""wfviz — a living dashboard for Claude Code dynamic workflows.

Reads what a running workflow writes to disk *live* and serves an animated
graph of the fleet: one node per subagent with its model, effort, tools,
tokens and state, edges for data flow, and a click-through to each agent's
real transcript. Nothing is mocked — every field is read from the actual run.

Live data sources (verified to update in real time during a run):
  • subagents/workflows/wf_<id>/journal.jsonl   — per-agent started/result (+result value)
  • subagents/workflows/wf_<id>/agent-<id>.jsonl — the agent's live transcript
  • subagents/workflows/wf_<id>/agent-<id>.meta.json — model + agentType
Completion + enrichment (written only when the run ends):
  • workflows/wf_<id>.json — final workflowProgress, logs, tokens

Node identity (label/phase/effort/tools/deps) rides in each agent's prompt as a
⟦wfnode {...}⟧ header emitted by viz-preamble.js, so the labeled graph and its
edges are recoverable live from transcripts alone. Without the preamble you
still get a real graph keyed by agent id + prompt preview.

    python3 wfviz.py --port 8777      →  http://127.0.0.1:8777
"""
import argparse, glob, json, os, re, time
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
# Claude Code lays these out as  projects/<project>/<session>/{subagents,workflows}/…
SUBAGENTS_GLOB = os.path.expanduser("~/.claude/projects/*/*/subagents/workflows/wf_*")
WF_META_GLOB   = os.path.expanduser("~/.claude/projects/*/*/workflows")

TIERS = [("opus", "Opus", "opus"), ("sonnet", "Sonnet", "sonnet"),
         ("haiku", "Haiku", "haiku"), ("fable", "Fable", "fable")]
AGENT_TOOLS = {
    "general-purpose": ["Read", "Write", "Edit", "Bash", "Grep", "Glob", "Web"],
    "workflow-subagent": ["Read", "Grep", "Glob", "Bash", "Web"],
    "Explore": ["Read", "Grep", "Glob", "Web"],
    "code-reviewer": ["Read", "Grep", "Glob", "Bash"],
    "default": ["Read", "Grep", "Glob", "Bash", "Web"],
}
HDR = re.compile(r"^⟦wfnode\s+(\{.*?\})⟧", re.S)


def tier_of(model):
    """Precise display name for a model id: 'claude-opus-4-8' -> ('Opus 4.8','opus').

    Bare aliases ('opus') carry no version, so they render as the family alone;
    the full id from the transcript is preferred wherever it exists.
    """
    raw = (model or "").strip()
    m = raw.lower()
    if not m or m == "inherit":
        return "inherit", "inherit"
    key = next((k for needle, _lbl, k in TIERS if needle in m), "inherit")
    fam = next((lbl for needle, lbl, _k in TIERS if needle in m), None)
    if not fam:
        return raw, "inherit"
    ctx = "1M" if "[1m]" in m else None
    s = re.sub(r"\[1m\]", "", m)
    s = re.sub(r"^claude-", "", s)
    s = re.sub(r"-?\d{8}$", "", s)                 # drop the date stamp
    parts = [p for p in s.split("-") if p and p != fam.lower()]
    ver = ".".join(p for p in parts if p.isdigit())
    label = f"{fam} {ver}".strip() if ver else fam
    if ctx:
        label += f" ({ctx})"
    return label, key


def session_effort(run_dir):
    """Reasoning effort a subagent inherits when the workflow doesn't set one.
    Recorded in the launching session's own transcript, not the subagent's."""
    try:
        sid = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(run_dir))))
        for p in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl")):
            with open(p) as f:
                for line in f:
                    m = re.search(r'"effort"\s*:\s*"([a-z]+)"', line)
                    if m:
                        return m.group(1)
    except Exception:
        pass
    return None


def ts_ms(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def now_ms():
    return int(time.time() * 1000)


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def parse_header(prompt):
    """Extract a ⟦wfnode {...}⟧ header from a prompt; return (meta|None, clean_prompt)."""
    if not isinstance(prompt, str):
        return None, prompt
    m = HDR.match(prompt.lstrip())
    if not m:
        return None, prompt
    try:
        meta = json.loads(m.group(1))
    except Exception:
        return None, prompt
    clean = prompt.lstrip()[m.end():].lstrip("\n")
    return meta, clean


def _flatten(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content) if content is not None else ""


def parse_transcript(path, full=False):
    """Parse an agent-<id>.jsonl into prompt, a clean event timeline, tokens, timing.
    Tolerant of partial last lines (live files)."""
    prompt = None
    events, usage = [], None
    started = last = None
    tool_calls = 0
    real_model = None
    try:
        raw = open(path).read()
    except Exception:
        return None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue                      # partial final line during a live write
        t = ts_ms(d.get("timestamp"))
        if t:
            started = started or t
            last = t
        typ = d.get("type")
        if typ == "attachment":           # hook/context injections — not agent activity
            continue
        msg = d.get("message") or {}
        role = msg.get("role")
        c = msg.get("content")
        if msg.get("usage"):
            usage = msg["usage"]
        if msg.get("model") and not str(msg["model"]).startswith("<"):
            real_model = msg["model"]        # full id, e.g. claude-opus-4-8
        if role == "user" and isinstance(c, str):
            if prompt is None:
                hdr, clean = parse_header(c)
                prompt = clean
                if full:
                    events.append({"t": t, "kind": "prompt", "text": clean})
            elif full:
                events.append({"t": t, "kind": "note", "text": c})
        elif role == "user" and isinstance(c, list) and full:
            for b in c:
                if b.get("type") == "tool_result":
                    events.append({"t": t, "kind": "tool_result", "text": _flatten(b.get("content"))[:4000]})
        elif role == "assistant" and isinstance(c, list):
            for b in c:
                bt = b.get("type")
                if bt == "tool_use":
                    tool_calls += 1
                    if full:
                        events.append({"t": t, "kind": "tool_use", "name": b.get("name"),
                                       "input": b.get("input")})
                elif full and bt == "thinking":
                    events.append({"t": t, "kind": "thinking", "text": b.get("thinking", "")})
                elif full and bt == "text":
                    events.append({"t": t, "kind": "text", "text": b.get("text", "")})
    tok = 0
    out = 0
    if usage:
        out = usage.get("output_tokens", 0)
        tok = (usage.get("input_tokens", 0) + out + usage.get("cache_read_input_tokens", 0)
               + usage.get("cache_creation_input_tokens", 0))
    return {"prompt": prompt or "", "events": events, "tokens": tok, "outTokens": out,
            "started": started, "last": last, "toolCalls": tool_calls, "model": real_model}


def read_journal(run_dir):
    """agentId -> {'state':'done'|'running', 'result':...}"""
    out = {}
    p = os.path.join(run_dir, "journal.jsonl")
    try:
        raw = open(p).read()
    except Exception:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        aid = d.get("agentId")
        if not aid:
            continue
        if d.get("type") == "started":
            out.setdefault(aid, {"state": "running", "result": None})
        elif d.get("type") == "result":
            out[aid] = {"state": "done", "result": d.get("result")}
    return out


def runid_of(run_dir):
    return os.path.basename(run_dir)


def find_config(runid):
    """The workflows/<runid>.json final record — exists only once the run completes."""
    for base in glob.glob(WF_META_GLOB):
        p = os.path.join(base, runid + ".json")
        if os.path.exists(p):
            return read_json(p)
    return None


def script_meta(runid):
    """name + ordered phase titles, parsed from the on-disk script (exists during the run)."""
    for base in glob.glob(WF_META_GLOB):
        for sp in glob.glob(os.path.join(base, "scripts", "*" + runid + ".js")):
            try:
                s = open(sp).read()
            except Exception:
                continue
            name = None
            m = re.search(r"name:\s*'([^']+)'|name:\s*\"([^\"]+)\"", s)
            if m:
                name = m.group(1) or m.group(2)
            phases = re.findall(r"title:\s*'([^']+)'|title:\s*\"([^\"]+)\"", s)
            phases = [a or b for a, b in phases]
            return {"name": name, "phases": phases}
    return {"name": None, "phases": []}


def build_graph(run_dir, collapse=None):
    runid = runid_of(run_dir)
    journal = read_journal(run_dir)
    sess_eff = session_effort(run_dir)
    sm = script_meta(runid)
    config = find_config(runid)
    done_run = config is not None            # config json appears only at completion

    agent_files = sorted(glob.glob(os.path.join(run_dir, "agent-*.jsonl")))
    raw = []
    for af in agent_files:
        aid = os.path.basename(af)[len("agent-"):-len(".jsonl")]
        meta = read_json(af[:-len(".jsonl")] + ".meta.json", {}) or {}
        tr = parse_transcript(af) or {"prompt": "", "tokens": 0, "started": None, "last": None,
                                      "toolCalls": 0, "outTokens": 0}
        hdr, _ = parse_header(_first_user(af))
        raw.append({"aid": aid, "meta": meta, "tr": tr, "hdr": hdr})

    # Execution order is the only ordering we can always trust: the script file
    # is only scrapeable for inline-launched runs, and agent-*.jsonl sorts by id
    # hash. Without this, phases get numbered in hash order (Probe=1, Scope=3),
    # which scrambles the columns and makes forward edges look like back-edges.
    raw.sort(key=lambda r: (r["tr"].get("started") is None, r["tr"].get("started") or 0))

    # phase title -> index: script literals, else the run's own recorded phases
    phase_titles = sm["phases"] or [p.get("title") for p in (config or {}).get("phases", [])
                                    if p.get("title")]
    title_idx = {t: i + 1 for i, t in enumerate(phase_titles) if t}
    seq = {}

    def pidx(name):
        if name in title_idx:
            return title_idx[name]
        if name not in seq:
            seq[name] = (max([*title_idx.values(), *seq.values(), 0]) + 1)
        return seq[name]

    nodes, id_by_aid, edges = [], {}, []
    used = {}
    for r in raw:
        aid, meta, tr, hdr = r["aid"], r["meta"], r["tr"], r["hdr"]
        model = tr.get("model") or meta.get("model") or (hdr or {}).get("model") or "inherit"
        tier_label, tier_key = tier_of(model)
        label = (hdr or {}).get("id")
        if not label:
            # no preamble: name the node from what it was actually asked to do
            words = re.sub(r"[^\w\s-]", " ", (tr.get("prompt") or "")).split()
            label = " ".join(words[:4])[:34].strip() or ("agent " + aid[:6])
        if label in used:                     # keep ids unique for edge wiring
            used[label] += 1
            label = f"{label} ({used[label]})"
        else:
            used[label] = 1
        phase = (hdr or {}).get("phase")
        agent_type = meta.get("agentType") or "default"
        tools = (hdr or {}).get("tools")
        inferred = tools is None
        if inferred:
            tools = AGENT_TOOLS.get(agent_type, AGENT_TOOLS["default"])
        jt = journal.get(aid)
        state = jt["state"] if jt else ("running" if tr.get("started") else "queued")
        # An agent with a `started` entry and no `result` is orphaned, not running.
        # Once the run is over it can never finish, so leaving it 'running' pinned
        # its duration to now() and blew up the run's wall clock (observed: 29 days).
        if done_run and state != "done":
            state = "error"
        started, last = tr.get("started"), tr.get("last")
        dur = (last - started) if (started and last and state in ("done", "error")) else \
              ((now_ms() - started) if started and state == "running" else None)
        id_by_aid[aid] = label
        nodes.append({
            "id": label, "agentId": aid, "phase": phase,
            "phaseIndex": pidx(phase) if phase else None,
            "model": model, "tier": tier_label, "tierKey": tier_key,
            "state": state, "effort": (hdr or {}).get("effort", "inherit"),
            "tools": tools, "inferredTools": inferred, "agentType": agent_type,
            "tokens": tr.get("tokens", 0), "outTokens": tr.get("outTokens", 0),
            "toolCalls": tr.get("toolCalls", 0), "durationMs": dur, "startedAt": started,
            "promptPreview": (tr.get("prompt") or "")[:160],
            "resultPreview": json.dumps(jt["result"])[:200] if jt and jt.get("result") is not None else "",
            "deps": (hdr or {}).get("deps") or [],
            # course steps 8-11: routers, verifier gates, reducers, isolated nodes
            "role": (hdr or {}).get("role"),
            "isolated": bool((hdr or {}).get("isolation")),
            "hasHeader": hdr is not None,
        })

    have_headers = any(n["hasHeader"] for n in nodes)
    # assign phaseIndex to header-less nodes by concurrency buckets (start-time layering)
    if not have_headers or any(n["phaseIndex"] is None for n in nodes):
        _bucket_phases(nodes)

    label_set = {n["id"] for n in nodes}
    warnings = []
    if have_headers:                          # explicit edges from each node's deps
        for n in nodes:
            for d in n["deps"]:
                if d == n["id"]:
                    continue
                if d in label_set:
                    edges.append({"from": d, "to": n["id"]})
                else:                          # don't drop a broken edge silently
                    warnings.append(f"{n['id']}: dep '{d}' matches no node")
    if not edges:                             # fallback: connect consecutive phase layers
        edges = _phase_edges(nodes)
    _edge_kinds(edges, nodes)

    # what actually crossed each inbound edge (vnodeFrom delimits it in the prompt)
    for r in raw:
        lbl = (r["hdr"] or {}).get("id")
        if not lbl:
            continue
        pay = edge_payloads(_first_user(os.path.join(run_dir, f"agent-{r['aid']}.jsonl")))
        for n in nodes:
            if n["id"] == lbl:
                n["inbound"] = {k: v[:1200] for k, v in pay.items()}
                break
    for n in nodes:
        n.setdefault("inbound", {})
    carried = {(k, n["id"]) for n in nodes for k in n["inbound"]}
    for e in edges:                            # did data really move, or is it decorative?
        e["carried"] = (e["from"], e["to"]) in carried

    # Series reduction when the graph is too deep to fit on screen. Purely visual:
    # a degree-2 run carries no branching, so collapsing it loses no structure.
    ncols = len({n["phaseIndex"] for n in nodes})
    chains = []
    if collapse is None:
        collapse = ncols > 14           # ~ wider than a screen can show legibly
    if collapse:
        nodes, edges, chains = contract_chains(nodes, edges)
        _edge_kinds(edges, nodes)

    cpath, cspan, par, slack = critical_path(nodes, edges)
    cset = set(cpath)
    for n in nodes:
        n["onCritical"] = n["id"] in cset
        n["slackMs"] = slack.get(n["id"], 0)
    for e in edges:
        e["onCritical"] = e["from"] in cset and e["to"] in cset and \
            abs(cpath.index(e["to"]) - cpath.index(e["from"])) == 1 \
            if (e["from"] in cset and e["to"] in cset) else False

    phases = [{"index": i + 1, "title": t} for i, t in enumerate(phase_titles)]
    seen_pi = {n["phaseIndex"] for n in nodes}
    for pi in sorted(seen_pi):
        if not any(p["index"] == pi for p in phases):
            phases.append({"index": pi, "title": next((n["phase"] for n in nodes
                            if n["phaseIndex"] == pi and n["phase"]), f"phase {pi}")})
    phases.sort(key=lambda p: p["index"])

    st = {"done": 0, "running": 0, "queued": 0, "error": 0, "planned": 0}
    for n in nodes:
        st[n["state"]] = st.get(n["state"], 0) + 1
    status = "done" if done_run else ("running" if nodes else "planned")
    starts = [n["startedAt"] for n in nodes if n["startedAt"]]
    ends = [n["startedAt"] + n["durationMs"] for n in nodes if n["startedAt"] and n["durationMs"]]
    lo = min(starts) if starts else None
    hi = max(ends) if ends and status == "done" else now_ms()
    elapsed = (hi - lo) if lo else 0
    total_tokens = (config or {}).get("totalTokens") or sum(n["tokens"] for n in nodes)

    return {
        "run": runid, "name": sm["name"] or (config or {}).get("workflowName") or runid,
        "status": status, "phases": phases, "nodes": nodes, "edges": edges,
        "warnings": warnings,
        "collapsed": bool(chains), "chains": len(chains),
        "critical": {"path": cpath, "spanMs": cspan, "parallelism": par},
        "totals": {"agents": len(nodes), "tokens": total_tokens,
                   "toolCalls": sum(n["toolCalls"] for n in nodes),
                   "elapsedMs": elapsed, "criticalMs": cspan, "parallelism": par,
                   "carriedEdges": sum(1 for e in edges if e.get("carried")),
                   "edges": len(edges), **st},
    }


def _first_user(af):
    try:
        for line in open(af):
            d = json.loads(line)
            msg = d.get("message") or {}
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                return msg["content"]
    except Exception:
        pass
    return ""


def _bucket_phases(nodes):
    started = [n for n in nodes if n["startedAt"]]
    started.sort(key=lambda n: n["startedAt"])
    layer, last_t, idx = {}, None, 0
    for n in started:
        if last_t is None or n["startedAt"] - last_t > 700:
            idx += 1
            last_t = n["startedAt"]
        n["phaseIndex"] = n["phaseIndex"] or idx
    for n in nodes:
        n["phaseIndex"] = n["phaseIndex"] or (idx + 1)


def _phase_edges(nodes):
    layers = {}
    for n in nodes:
        layers.setdefault(n["phaseIndex"], []).append(n["id"])
    order = sorted(layers)
    edges = []
    for a, b in zip(order, order[1:]):
        src, tgt = layers[a], layers[b]
        if len(src) == 1:
            edges += [{"from": src[0], "to": t} for t in tgt]
        elif len(tgt) == 1:
            edges += [{"from": s, "to": tgt[0]} for s in src]
        else:
            edges += [{"from": s, "to": tgt[i % len(tgt)]} for i, s in enumerate(src)]
    return edges


UPSTREAM_RE = re.compile(r"---\s*upstream\s*·\s*(.+?)\s*---\n(.*?)(?=\n---\s*upstream\s*·|\Z)", re.S)


def edge_payloads(prompt):
    """What actually crossed each inbound edge. vnodeFrom() delimits every
    upstream result in the prompt, so the payload is recoverable verbatim."""
    return {m.group(1).strip(): m.group(2).strip() for m in UPSTREAM_RE.finditer(prompt or "")}


def contract_chains(nodes, edges, minlen=3):
    """Series reduction (path contraction): a maximal run of nodes each having
    exactly one predecessor and one successor carries no branching information,
    so it can be drawn as ONE node without losing structure.

    This is what makes deep runs viewable: the largest run on disk is a 126-column
    serial chain — a 40,000px canvas that cannot be fitted on screen. Contracted,
    it becomes a handful of columns. Returns (nodes, edges) rewired.
    """
    ids = {n["id"] for n in nodes}
    succ, pred = {}, {}
    for e in edges:
        succ.setdefault(e["from"], []).append(e["to"])
        pred.setdefault(e["to"], []).append(e["from"])
    interior = {i for i in ids
                if len(succ.get(i, [])) == 1 and len(pred.get(i, [])) == 1}

    by_id = {n["id"]: n for n in nodes}
    seen, chains = set(), []
    for i in ids:                                  # walk each maximal interior run
        if i not in interior or i in seen:
            continue
        chain = [i]
        seen.add(i)
        cur = i
        while True:                                 # extend forward
            nxt = succ.get(cur, [None])[0]
            if nxt in interior and nxt not in seen:
                seen.add(nxt); chain.append(nxt); cur = nxt
            else:
                break
        cur = i
        while True:                                 # extend backward
            prv = pred.get(cur, [None])[0]
            if prv in interior and prv not in seen:
                seen.add(prv); chain.insert(0, prv); cur = prv
            else:
                break
        if len(chain) >= minlen:
            chains.append(chain)

    if not chains:
        return nodes, edges, []

    collapsed, member_of = [], {}
    for c in chains:
        members = [by_id[x] for x in c]
        mid = f"⋯ {len(c)} steps"
        base = members[0]
        cid = f"{base['id']} ⋯ +{len(c)-1}"
        for x in c:
            member_of[x] = cid
        collapsed.append({
            **base, "id": cid, "collapsed": True, "members": [m["id"] for m in members],
            "memberCount": len(members),
            "tokens": sum(m.get("tokens") or 0 for m in members),
            "toolCalls": sum(m.get("toolCalls") or 0 for m in members),
            "durationMs": sum(m.get("durationMs") or 0 for m in members),
            "state": ("running" if any(m["state"] == "running" for m in members)
                      else "error" if any(m["state"] == "error" for m in members)
                      else "done" if all(m["state"] == "done" for m in members) else "queued"),
            "promptPreview": base.get("promptPreview", ""),
            "resultPreview": members[-1].get("resultPreview", ""),
            "phase": base.get("phase"), "phaseIndex": base.get("phaseIndex"),
        })

    keep = [n for n in nodes if n["id"] not in member_of] + collapsed
    remap = lambda x: member_of.get(x, x)
    out, dedup = [], set()
    for e in edges:
        a, b = remap(e["from"]), remap(e["to"])
        if a == b or (a, b) in dedup:
            continue
        dedup.add((a, b))
        out.append({**e, "from": a, "to": b})
    return keep, out, [c for c in chains]


def critical_path(nodes, edges):
    """Longest-duration chain through the DAG — the path that sets the makespan.
    Everything off it has slack; shortening anything off it changes nothing."""
    dur = {n["id"]: (n.get("durationMs") or 0) for n in nodes}
    preds = {}
    for e in edges:
        if e.get("kind") != "cycle":            # ignore back-edges so this terminates
            preds.setdefault(e["to"], []).append(e["from"])
    order = sorted(nodes, key=lambda n: ((n.get("phaseIndex") or 0), n.get("startedAt") or 0))
    best, prev = {}, {}
    for n in order:
        nid = n["id"]
        cands = [(best.get(p, 0), p) for p in preds.get(nid, []) if p in best]
        if cands:
            b, p = max(cands)
            best[nid], prev[nid] = b + dur.get(nid, 0), p
        else:
            best[nid], prev[nid] = dur.get(nid, 0), None
    if not best:
        return [], 0, 1.0, {}
    end = max(best, key=lambda k: best[k])
    path, cur = [], end
    while cur:
        path.append(cur)
        cur = prev.get(cur)
    path.reverse()
    span = best[end]
    total = sum(dur.values())

    # Backward pass -> total float (slack): how long a node could overrun before
    # it starts delaying the whole run. Zero slack == on the critical path.
    succ = {}
    for e in edges:
        if e.get("kind") != "cycle":
            succ.setdefault(e["from"], []).append(e["to"])
    latest = {}
    for n in reversed(order):
        nid = n["id"]
        outs = [latest.get(s) for s in succ.get(nid, []) if s in latest]
        latest[nid] = min([o - dur.get(s, 0) for o, s in
                           zip(outs, [s for s in succ.get(nid, []) if s in latest])],
                          default=span)
    slack = {nid: max(0, latest.get(nid, span) - best.get(nid, 0)) for nid in best}
    return path, span, (round(total / span, 2) if span else 1.0), slack


def _edge_kinds(edges, nodes):
    indeg, outdeg, adj = {}, {}, {}
    for e in edges:
        outdeg[e["from"]] = outdeg.get(e["from"], 0) + 1
        indeg[e["to"]] = indeg.get(e["to"], 0) + 1
        adj.setdefault(e["from"], []).append(e["to"])
    # Real back-edges via DFS colouring. The old test (target phase <= source
    # phase) is not cycle detection: it flagged 6 of 13 edges in an acyclic run,
    # including scope -> probe:cost, whenever phase indices were unreliable.
    color, back = {}, set()
    for root in [n["id"] for n in nodes]:
        if color.get(root, 0):
            continue
        stack = [(root, iter(adj.get(root, [])))]
        color[root] = 1
        while stack:
            u, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[u] = 2
                stack.pop()
                continue
            c = color.get(nxt, 0)
            if c == 1:
                back.add((u, nxt))          # points at a node still on the stack
            elif c == 0:
                color[nxt] = 1
                stack.append((nxt, iter(adj.get(nxt, []))))
    for e in edges:
        if (e["from"], e["to"]) in back:
            e["kind"] = "cycle"
        elif indeg.get(e["to"], 0) > 1:
            e["kind"] = "merge"
        elif outdeg.get(e["from"], 0) > 1:
            e["kind"] = "fanout"
        else:
            e["kind"] = "flow"


def build_agent(run_dir, aid):
    af = os.path.join(run_dir, f"agent-{aid}.jsonl")
    if not os.path.exists(af):
        return {"error": "no such agent"}
    meta = read_json(af[:-len(".jsonl")] + ".meta.json", {}) or {}
    tr = parse_transcript(af, full=True) or {}
    hdr, _ = parse_header(_first_user(af))
    journal = read_journal(run_dir)
    jt = journal.get(aid)
    model = meta.get("model") or (hdr or {}).get("model") or "inherit"
    tl, tk = tier_of(model)
    return {
        "agentId": aid, "id": (hdr or {}).get("id") or ("agent " + aid[:6]),
        "phase": (hdr or {}).get("phase"), "model": model, "tier": tl, "tierKey": tk,
        "agentType": meta.get("agentType") or "default",
        "effort": (hdr or {}).get("effort", "inherit"),
        "tools": (hdr or {}).get("tools") or AGENT_TOOLS.get(meta.get("agentType") or "default", AGENT_TOOLS["default"]),
        "state": jt["state"] if jt else ("running" if tr.get("started") else "queued"),
        "tokens": tr.get("tokens", 0), "outTokens": tr.get("outTokens", 0),
        "toolCalls": tr.get("toolCalls", 0),
        "startedAt": tr.get("started"), "lastAt": tr.get("last"),
        "durationMs": (tr["last"] - tr["started"]) if tr.get("started") and tr.get("last") else None,
        "prompt": tr.get("prompt", ""),
        "result": jt["result"] if jt else None,
        "events": tr.get("events", []),
    }


CHAT_DIR = os.path.expanduser("~/.wfviz")
SESSION_GLOB = os.path.expanduser("~/.claude/projects/*/*.jsonl")


def chat_path(sid):
    """Two-way message log between the browser and the live Claude session."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", sid or "default")
    return os.path.join(CHAT_DIR, f"chat-{safe}.jsonl")


def chat_read(sid):
    out = []
    try:
        with open(chat_path(sid)) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return out


def chat_append(sid, role, text):
    os.makedirs(CHAT_DIR, exist_ok=True)
    with open(chat_path(sid), "a") as f:
        f.write(json.dumps({"role": role, "text": text, "t": now_ms()}) + "\n")
    return True
SYSREM = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def session_feed(session_id, limit=40, tail_bytes=400_000):
    """Live read-only feed of a Claude Code session's own transcript.
    Lets the browser follow the ACTIVE session even when it isn't tmux-hosted
    (a running session can't be mirrored as a terminal, but it is on disk)."""
    path = next((p for p in glob.glob(SESSION_GLOB)
                 if os.path.basename(p) == session_id + ".jsonl"), None)
    if not path:
        return {"error": "no transcript for session", "id": session_id}
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > tail_bytes:
            f.seek(size - tail_bytes)
        raw = f.read().decode("utf-8", "ignore")
    lines = raw.splitlines()
    if size > tail_bytes and lines:
        lines = lines[1:]                      # drop the partial first line
    events = []
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        msg = d.get("message") or {}
        role, c, t = msg.get("role"), msg.get("content"), ts_ms(d.get("timestamp"))
        if role == "user" and isinstance(c, str):
            txt = SYSREM.sub("", c).strip()
            if txt and not txt.startswith("Caveat:"):
                events.append({"t": t, "kind": "user", "text": txt[:1500]})
        elif role == "assistant" and isinstance(c, list):
            for b in c:
                bt = b.get("type")
                if bt == "text" and (b.get("text") or "").strip():
                    events.append({"t": t, "kind": "assistant", "text": b["text"][:1500]})
                elif bt == "tool_use":
                    events.append({"t": t, "kind": "tool", "name": b.get("name"),
                                   "text": json.dumps(b.get("input"))[:220]})
    return {"id": session_id, "events": events[-limit:],
            "mtime": int(os.path.getmtime(path) * 1000)}


def discover(session=None):
    """Newest-first workflow run dirs. `session` scopes to one Claude session id
    (the layout is projects/<project>/<session>/subagents/workflows/wf_*)."""
    runs = []
    for d in glob.glob(SUBAGENTS_GLOB):
        if not os.path.isdir(d):
            continue
        if session and f"{os.sep}{session}{os.sep}subagents{os.sep}" not in d:
            continue
        files = glob.glob(os.path.join(d, "agent-*.jsonl")) + [os.path.join(d, "journal.jsonl")]
        mt = max((os.path.getmtime(f) for f in files if os.path.exists(f)), default=0)
        if mt:
            runs.append((mt, d))
    runs.sort(reverse=True)
    return runs


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _run_dir(self, want, session=None):
        runs = discover(session)
        if want:
            return next((d for _, d in runs if runid_of(d) == want), None)
        return runs[0][1] if runs else None

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path != "/chat":
            return self._send(404, json.dumps({"error": "not found"}))
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"error": "bad body"}))
        sid = q.get("id", [body.get("id")])[0]
        text = (body.get("text") or "").strip()
        if not sid or not text:
            return self._send(400, json.dumps({"error": "need id + text"}))
        chat_append(sid, body.get("role") or "user", text)
        return self._send(200, json.dumps({"ok": True}))

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        sess = q.get("session", [None])[0]
        if u.path == "/runs":
            out = []
            for mt, d in discover(sess)[:40]:
                rid = runid_of(d)
                sm = script_meta(rid)
                cfg = find_config(rid)
                agents = len(glob.glob(os.path.join(d, "agent-*.jsonl")))
                out.append({"run": rid,
                            "name": sm["name"] or (cfg or {}).get("workflowName") or rid,
                            "status": "done" if cfg else "running",
                            "agents": agents,
                            "tokens": (cfg or {}).get("totalTokens") or 0,
                            "mtime": int(mt * 1000)})
            return self._send(200, json.dumps(out))
        if u.path == "/compare":
            a, b = q.get("a", [None])[0], q.get("b", [None])[0]
            da, db = self._run_dir(a, sess), self._run_dir(b, sess)
            if not da or not db:
                return self._send(200, json.dumps({"error": "need two run ids"}))
            ga, gb = build_graph(da), build_graph(db)
            byb = {n["id"]: n for n in gb["nodes"]}
            rows = []
            for n in ga["nodes"]:
                m = byb.get(n["id"])
                rows.append({"id": n["id"],
                             "durA": n.get("durationMs"), "durB": m and m.get("durationMs"),
                             "tokA": n.get("tokens"), "tokB": m and m.get("tokens"),
                             "only": None if m else "a"})
            for n in gb["nodes"]:
                if n["id"] not in {x["id"] for x in ga["nodes"]}:
                    rows.append({"id": n["id"], "durA": None, "durB": n.get("durationMs"),
                                 "tokA": None, "tokB": n.get("tokens"), "only": "b"})
            return self._send(200, json.dumps({
                "a": {"run": ga["run"], "name": ga["name"], "totals": ga["totals"]},
                "b": {"run": gb["run"], "name": gb["name"], "totals": gb["totals"]},
                "rows": rows}))
        if u.path == "/state":
            d = self._run_dir(q.get("run", [None])[0], sess)
            if not d:
                return self._send(200, json.dumps({"empty": True}))
            try:
                return self._send(200, json.dumps(build_graph(d)))
            except Exception as e:
                return self._send(200, json.dumps({"error": repr(e)}))
        if u.path == "/session":
            sid = q.get("id", [sess])[0]
            if not sid:
                return self._send(200, json.dumps({"error": "need id"}))
            return self._send(200, json.dumps(session_feed(sid)))
        if u.path == "/chat":
            sid = q.get("id", [sess])[0]
            msgs = chat_read(sid)
            pending = sum(1 for m in msgs if m.get("role") == "user") - \
                      sum(1 for m in msgs if m.get("role") == "assistant")
            return self._send(200, json.dumps({"id": sid, "messages": msgs,
                                               "pending": max(0, pending)}))
        if u.path == "/agent":
            d = self._run_dir(q.get("run", [None])[0], sess)
            aid = q.get("id", [None])[0]
            if not d or not aid:
                return self._send(200, json.dumps({"error": "need run+id"}))
            return self._send(200, json.dumps(build_agent(d, aid)))
        return self._send(404, json.dumps({"error": "not found"}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8777)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"wfviz → http://127.0.0.1:{a.port}   (watching {SUBAGENTS_GLOB})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
