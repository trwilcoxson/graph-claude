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

SEV_ORDER = {"critical": 3, "high": 2, "medium": 1, "info": 0}


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

    # ── finding 1: untrusted data actually reached a privileged sink ──────
    for nid, p in posture.items():
        if not p["sink"]:
            continue
        srcs = p["taintFrom"] or ([nid] if p["ingress"] else [])
        if not srcs:
            continue
        gated = _path_has_verifier(srcs, nid, flow, nodes)
        sev = "high" if not gated else "medium"
        findings.append({
            "id": f"taint::{nid}",
            "node": nid, "severity": sev, "confidence": "possible",
            "title": "Untrusted content reached a privileged tool",
            "evidence": (f"{', '.join(srcs)} used {', '.join(sorted(set(sum([posture[s]['ingress'] for s in srcs if s in posture], []))))}"
                         f" and its output was injected into {nid}, which called {', '.join(p['sink'])}."
                         + ("" if gated else " No verifier node lies on that path.")),
            "why": "Content of unknown provenance can influence a world-changing action.",
            "path": _one_path(srcs[0], nid, flow) if srcs else [nid],
        })

    # ── finding 2: a tool argument traceable only to injected content ─────
    for nid, n in nodes.items():
        inbound = " \n".join((n.get("inbound") or {}).values())
        if not inbound:
            continue
        own = _distinctive(own_prompt.get(nid, ""))
        inj = _distinctive(inbound) - own
        if not inj:
            continue
        for tool, arg in tool_args.get(nid, []):
            if tool not in PRIVILEGED_SINK:
                continue
            hits = sorted(t for t in inj if t in (arg or ""))
            if hits:
                findings.append({
                    "id": f"authority::{nid}::{tool}",
                    "node": nid, "severity": "critical", "confidence": "confirmed",
                    "title": "Tool argument traceable to injected content",
                    "evidence": (f"{nid} called {tool} with {', '.join(hits[:3])}, which appears in the "
                                 f"payload injected from upstream but nowhere in the node's own task."),
                    "why": "The action was shaped by data the node received, not by its instructions.",
                    "path": _one_path((n.get("inbound") and list(n["inbound"])[0]) or nid, nid, flow),
                })

    # ── finding 3: capability exercised beyond what was declared ──────────
    for nid, p in posture.items():
        if p["undeclared"]:
            findings.append({
                "id": f"capability::{nid}",
                "node": nid, "severity": "medium", "confidence": "confirmed",
                "title": "Used capability it did not declare",
                "evidence": (f"{nid} declared {', '.join(nodes[nid].get('tools') or []) or 'nothing'} "
                             f"but called {', '.join(p['undeclared'])}. agentType "
                             f"'{nodes[nid].get('agentType')}' grants it regardless — the declaration is a label, not a sandbox."),
                "why": "Real capability exceeds stated intent, so the graph understates blast radius.",
                "path": [nid],
            })

    # ── finding 4: an ungated fan-out that routes around the verifier ─────
    for nid, p in posture.items():
        if p["sink"] and p["taintFrom"]:
            bypass = [s for s in p["taintFrom"] if not _path_has_verifier([s], nid, flow, nodes)]
            if bypass and any(_path_has_verifier([s], nid, flow, nodes) for s in p["taintFrom"]):
                findings.append({
                    "id": f"bypass::{nid}",
                    "node": nid, "severity": "high", "confidence": "confirmed",
                    "title": "A branch routes around the verifier",
                    "evidence": (f"Untrusted data reaches {nid} by several paths; the one from "
                                 f"{', '.join(bypass)} crosses no verifier while another does."),
                    "why": "A gate that only some paths cross is not a gate.",
                    "path": _one_path(bypass[0], nid, flow),
                })

    findings.sort(key=lambda f: -SEV_ORDER.get(f["severity"], 0))
    counts = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {
        "posture": posture,
        "findings": findings,
        "summary": {
            "counts": counts,
            "worst": findings[0]["severity"] if findings else None,
            "ingressNodes": sum(1 for p in posture.values() if p["ingress"]),
            "sinkNodes": sum(1 for p in posture.values() if p["sink"]),
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
