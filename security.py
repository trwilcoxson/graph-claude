"""security.py — risk analysis over a reconstructed workflow graph.

Turns the graph into an information-flow view: which nodes ingested untrusted
content, which hold privileged capability, whether untrusted data can actually
reach a privileged action, and whether a verifier sits on that path.

Design rules, each learned from a real run rather than assumed:

* Taint follows edges that ACTUALLY CARRIED DATA, never mere reachability. A
  WebFetch node was reachable to a Bash node in a real run, but nothing crossed
  the edge — the Bash calls were an agent hunting the filesystem for data a
  broken edge never delivered. Reporting that as an injection path is theatre.
* Over-privilege alone is not a finding. Measured: 10 of 12 real runs declare
  tools they never use. Warning on it trains the user to ignore the overlay.
  The sharp signal is the inverse — capability USED beyond what was declared.
* `tools` in the node header is a label, not a sandbox. Capability actually
  comes from agentType. Surfacing that gap is the point, not a bug to hide.
* Nothing here can prove influence. Evidence is graded, and the wording of a
  finding never overstates what the data supports.
"""
import re

# Tools that bring content of unknown provenance into an agent's context.
UNTRUSTED_INGRESS = {
    "WebFetch", "WebSearch",
    "mcp__playwright__browser_navigate", "mcp__playwright__browser_snapshot",
    "mcp__playwright__browser_evaluate", "mcp__claude-in-chrome__navigate",
    "mcp__claude-in-chrome__read_page", "mcp__claude-in-chrome__get_page_text",
}
# Tools that act on the world. Reaching one of these with untrusted data is the risk.
PRIVILEGED_SINK = {
    "Bash", "Write", "Edit", "NotebookEdit",
    "mcp__playwright__browser_run_code_unsafe",
}
# Heuristic: nothing in the format marks a checkpoint, so gates are named.
VERIFIER_HINT = re.compile(r"verif|review|check|judge|gate|audit|adjudic", re.I)

# Evidence-strength ladder, not a risk score. A reviewer needs to know whether
# they are looking at an architecture smell or a live event, so every finding
# names its tier. Deliberately NOT called "critical/high/medium": "critical"
# sits one button away from "critical path" in this UI and would be misread.
TIER_RANK = {"OBSERVED": 2, "REACHABLE": 1, "DECLARED": 0}
TIER_HELP = {
    "OBSERVED":  "we saw the actual call and matched its argument to upstream text",
    "REACHABLE": "data really crossed the edge and could have influenced this",
    "DECLARED":  "a claim about capability, not an observation of misuse",
}

# Two hard ceilings make some claims unsupportable at every tier: extended
# thinking ships encrypted, so intent is structurally invisible; and this is a
# read-only tailer, so nothing was ever intercepted. Enforce in code, not prose.
BANNED_CLAIMS = re.compile(
    r"\b(blocked|prevented|stopped|mitigated|malicious|attack(?:er)?|intent(?:ional)?|"
    r"deliberate|exploit(?:ed)?|compromis(?:ed|e)|breach|safe|secure|proves?)\b", re.I)


def _check_claims(f):
    """Fail loudly rather than ship an overstated finding."""
    for field in ("title", "evidence", "why"):
        m = BANNED_CLAIMS.search(f.get(field) or "")
        assert not m, f"unsupportable claim {m.group(0)!r} in {f['id']}.{field}"
    return f


def classify_tools(names):
    return {
        "ingress": sorted(n for n in names if n in UNTRUSTED_INGRESS),
        "sink": sorted(n for n in names if n in PRIVILEGED_SINK),
    }


def _distinctive(text):
    """Tokens a coincidence would not produce. Prose words are useless here: an
    early build matched 'critical', 'existing', 'parallel' and produced 21 false
    criticals across 14 runs. Only STRUCTURAL tokens count — a URL, a real path,
    a command flag, or a dotted/underscored identifier."""
    if not text:
        return set()
    pats = (
        r"https?://[^\s\"']+",              # url
        r"(?:/[\w.-]+){2,}",                # path with at least two segments
        r"--[a-zA-Z][\w-]{2,}",             # long flag
        r"\b\w+\.(?:py|js|json|html|sh|md|jsonl|ts|css|yml|yaml)\b",   # filename
        r"\b[a-z]+_[a-z_]{3,}\b",           # snake_case identifier
    )
    toks = set()
    for p in pats:
        for m in re.finditer(p, text):
            t = m.group(0).strip(".,;:)\"'")
            if len(t) >= 6:
                toks.add(t)
    return toks


def is_verifier(node):
    return (node.get("role") == "verifier"
            or bool(VERIFIER_HINT.search(node.get("id") or ""))
            or bool(VERIFIER_HINT.search(node.get("phase") or "")))


def analyze(graph, tools_used, tool_args, own_prompt):
    """graph: build_graph() output. tools_used: {node_id: {tool names}}.
    tool_args: {node_id: [(tool, argtext)]}. own_prompt: {node_id: task text
    with injected upstream payload stripped}."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    edges = graph.get("edges", [])

    # data-flow adjacency: only edges that actually carried a payload
    flow = {}
    for e in edges:
        if e.get("carried"):
            flow.setdefault(e["from"], []).append(e["to"])

    def reach(src, adj):
        seen, stack = set(), [src]
        while stack:
            x = stack.pop()
            for y in adj.get(x, []):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return seen

    posture, findings = {}, []
    for nid, n in nodes.items():
        used = set(tools_used.get(nid, ()))
        cls = classify_tools(used)
        declared = set(n.get("tools") or ())
        inferred = n.get("inferredTools", True)
        # only meaningful when the author really declared something
        undeclared = sorted(used - declared) if not inferred else []
        posture[nid] = {
            "ingress": cls["ingress"], "sink": cls["sink"],
            "undeclared": [u for u in undeclared if u in PRIVILEGED_SINK or u in UNTRUSTED_INGRESS],
            "tainted": False, "taintFrom": [], "exposure": "none",
        }

    # propagate taint along carrying edges from every ingress node
    for nid, p in posture.items():
        if p["ingress"]:
            for d in reach(nid, flow):
                posture[d]["tainted"] = True
                if nid not in posture[d]["taintFrom"]:
                    posture[d]["taintFrom"].append(nid)

    for nid, p in posture.items():
        if p["sink"] and (p["tainted"] or p["ingress"]):
            p["exposure"] = "reached"
        elif p["sink"]:
            p["exposure"] = "privileged"
        elif p["ingress"] or p["tainted"]:
            p["exposure"] = "untrusted"

    # ── finding 1: untrusted content crossed an EDGE into a privileged tool ──
    # A node that both fetches and executes is ordinary (a research agent with
    # web + bash). That is posture, shown as icons, not an alert. The alert is
    # for untrusted data travelling ACROSS a node boundary into a sink.
    for nid, p in posture.items():
        srcs = [s for s in p["taintFrom"] if s != nid]
        if not (p["sink"] and srcs):
            continue
        gated = _path_has_verifier(srcs, nid, flow, nodes)
        ing = sorted({t for s in srcs for t in posture[s]["ingress"]})
        findings.append({
            "id": f"taint::{nid}",
            "node": nid, "tier": "REACHABLE",
            "confidence": "possible", "gated": gated,
            "title": "Content of unknown provenance reached a privileged tool",
            "evidence": (f"{', '.join(srcs)} pulled content via {', '.join(ing)}; that output was "
                         f"injected into {nid}, which called {', '.join(p['sink'])}."
                         + (" A verifier lies on the path." if gated else " No verifier lies on that path.")),
            "why": "Content of unknown provenance can shape a world-changing action.",
            "path": _one_path(srcs[0], nid, flow),
        })

    # ── finding 2: a privileged argument traceable to UNTRUSTED injected text ──
    # Gating on taint is what makes this rare. Without it, every reduce/judge
    # node trips it simply by acting on a sibling's legitimate output.
    for nid, n in nodes.items():
        p = posture.get(nid, {})
        if not p.get("tainted"):
            continue
        inbound = " \n".join((n.get("inbound") or {}).values())
        if not inbound:
            continue
        inj = _distinctive(inbound) - _distinctive(own_prompt.get(nid, ""))
        if not inj:
            continue
        # one finding per (node, tool), tokens merged — eight near-identical
        # criticals for the same Bash node is spam, not eight problems.
        per_tool = {}
        for tool, arg in tool_args.get(nid, []):
            if tool not in PRIVILEGED_SINK:
                continue
            for t in inj:
                if t in (arg or ""):
                    per_tool.setdefault(tool, set()).add(t)
        for tool, hits in per_tool.items():
            # Taint through an LLM is weak: an agent that read the web and wrote
            # a summary may carry nothing across. Only a verbatim web artefact
            # (a URL) one hop from ingress is strong enough to call confirmed.
            direct = any(s in flow and nid in flow.get(s, []) for s in p["taintFrom"])
            url_like = any(h.startswith("http") for h in hits)
            confirmed = url_like and direct
            toks = sorted(hits)[:4]
            findings.append({
                "id": f"authority::{nid}::{tool}",
                "node": nid,
                "tier": "OBSERVED",
                "confidence": "confirmed" if confirmed else "possible",
                "title": "Privileged argument matches upstream-supplied text",
                "evidence": (f"{nid} called {tool} with {', '.join(toks)} — present in the payload "
                             f"injected from {', '.join(p['taintFrom'][:2])} (which pulled web "
                             f"content) and absent from {nid}'s own task."
                             + ("" if confirmed else " The token is not a verbatim web artefact, so "
                                "upstream influence is plausible, not established.")),
                "why": "The action may have been shaped by fetched data rather than by instructions.",
                "path": _one_path(p["taintFrom"][0], nid, flow),
            })

    # ── finding 3: undeclared capability, only where it actually matters ──
    # Measured: nodes exceed their declared tools in most runs, so a blanket
    # warning is wallpaper. Alert only when the extra capability is a privileged
    # sink on a node holding untrusted data; report the rest as one posture note.
    drift = []
    for nid, p in posture.items():
        if not p["undeclared"]:
            continue
        risky = [t for t in p["undeclared"] if t in PRIVILEGED_SINK]
        if risky and (p["tainted"] or p["ingress"]):
            findings.append({
                "id": f"capability::{nid}",
                "node": nid, "tier": "DECLARED", "confidence": "confirmed",
                "title": "Undeclared privileged tool on a node holding fetched content",
                "evidence": (f"{nid} declared {', '.join(nodes[nid].get('tools') or []) or 'nothing'} "
                             f"but called {', '.join(risky)}, while holding fetched content. "
                             f"agentType '{nodes[nid].get('agentType')}' grants it regardless."),
                "why": "Capability exceeds what was declared, exactly where it matters most.",
                "path": [nid],
            })
        else:
            drift.append(nid)

    # A 'verifier bypass' detector was written and deleted here. It was a strict
    # subset of taint:: — _path_has_verifier is False if ANY source path lacks a
    # verifier, so every node bypass:: could fire on had already produced a
    # taint:: card carrying the same sentence. Two cards, one fact.

    for f in findings:
        _check_claims(f)
    findings.sort(key=lambda f: (-TIER_RANK.get(f["tier"], 0), f["node"]))
    counts = {}
    for f in findings:
        counts[f["tier"]] = counts.get(f["tier"], 0) + 1
    notes = []
    if drift:
        notes.append(f"{len(drift)} node{'s' if len(drift) > 1 else ''} used tools beyond their "
                     f"declaration ({', '.join(drift[:4])}{'…' if len(drift) > 4 else ''}). "
                     f"Declared tools are a label; agentType grants the real capability.")
    return {
        "posture": posture,
        "findings": findings,
        "notes": notes,
        "summary": {
            "counts": counts,
            "worst": findings[0]["tier"] if findings else None,
            "ingressNodes": sum(1 for p in posture.values() if p["ingress"]),
            "sinkNodes": sum(1 for p in posture.values() if p["sink"]),
            "exposedNodes": sum(1 for p in posture.values() if p["ingress"] and p["sink"]),
            "clean": not findings,
        },
    }


def _one_path(src, dst, adj):
    prev, seen, stack = {}, {src}, [src]
    while stack:
        x = stack.pop()
        if x == dst:
            out = [dst]
            while out[-1] != src:
                out.append(prev[out[-1]])
            return list(reversed(out))
        for y in adj.get(x, []):
            if y not in seen:
                seen.add(y); prev[y] = x; stack.append(y)
    return [src, dst]


def _path_has_verifier(srcs, dst, adj, nodes):
    """True when EVERY carrying path from each source to dst crosses a verifier."""
    for s in srcs:
        for path in _all_paths(s, dst, adj):
            if not any(is_verifier(nodes.get(p, {})) for p in path[1:-1]):
                return False
    return True


def _all_paths(src, dst, adj, cap=64):
    out, stack = [], [(src, [src])]
    while stack and len(out) < cap:
        x, path = stack.pop()
        for y in adj.get(x, []):
            if y in path:
                continue
            if y == dst:
                out.append(path + [y])
            else:
                stack.append((y, path + [y]))
    return out
