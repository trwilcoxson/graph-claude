"""context.py — extend the agent graph into the system it actually touched.

The workflow DAG shows agents and the data passed between them. It stops at the
process boundary. But every agent also reaches outward: it reads and writes
files, runs commands, fetches URLs, calls services. Those reads and writes are
real edges — two agents touching the same file are coupled whether or not the
author declared a dependency.

This turns tool calls into a second graph layer:

  entity   a thing outside the workflow: a file, a directory, a host, a command,
           a search query, an MCP service.
  zone     how far out it sits: workspace, home, system, loopback, lan,
           internet, service. This is the "how far does this run reach" axis.
  edge     agent -> entity, typed by the operation (read/write/execute/fetch/
           search/call).

The derived part is the useful part:

  * implicit dependency — agent A writes a file, agent B later reads it. That is
    a real dependency that the declared DAG does not contain. It is the most
    common way a workflow is wrong in a way nobody can see.
  * write contention   — two agents writing the same path.
  * external landing   — an agent fetched from the internet and wrote to disk in
    the same run, so remote content reached the filesystem.

Every claim here comes from a recorded tool call. Nothing is inferred from
reachability alone, and nothing is reported that the transcript does not show.
"""
import os
import posixpath
import re
from urllib.parse import urlparse

HOME = os.path.expanduser("~")

# Commands that move data across a machine boundary; used to attribute a host to
# a Bash call rather than guessing from the binary alone.
NET_CMD = re.compile(r"\b(curl|wget|nc|ncat|ssh|scp|rsync|sftp|git|gh|docker|kubectl|psql|mysql|redis-cli|aws|gcloud|az)\b")
URL_RE = re.compile(r"https?://[^\s\"'`<>|]+")
HOST_ARG = re.compile(r"(?:@|//)([A-Za-z0-9._-]+\.[A-Za-z]{2,}|localhost|\d{1,3}(?:\.\d{1,3}){3})")
# A path that looks like a real filesystem path rather than a word with a slash.
PATH_RE = re.compile(r"(?<![\w-])((?:~|\.{1,2})?/[\w.@+~-]+(?:/[\w.@+~-]+)*)")

READERS = {"Read", "NotebookRead"}
WRITERS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}

LOOPBACK = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
PRIVATE_V4 = re.compile(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)")


def _abbrev(p):
    """Display paths without leaking the account name."""
    if p.startswith(HOME):
        return "~" + p[len(HOME):]
    return p


def zone_of_path(p):
    if p.startswith(HOME):
        return "home"
    if p.startswith(("/usr", "/etc", "/opt", "/bin", "/sbin", "/Library", "/System", "/var")):
        return "system"
    if p.startswith(("/tmp", "/private/tmp", "/private/var")):
        return "temp"
    return "system" if p.startswith("/") else "workspace"


def zone_of_host(h):
    h = (h or "").lower()
    if h in LOOPBACK:
        return "loopback"
    if PRIVATE_V4.match(h) or h.endswith(".local") or h.endswith(".internal"):
        return "lan"
    return "internet"


def _norm_path(raw, cwd=None):
    if not raw:
        return None
    p = raw.strip().strip("'\"")
    if p.startswith("~"):
        p = HOME + p[1:]
    if not p.startswith("/"):
        if not cwd:
            return None
        p = posixpath.normpath(posixpath.join(cwd, p))
    return posixpath.normpath(p)


def _entity(ents, kind, key, zone, label=None):
    eid = f"{kind}:{key}"
    e = ents.get(eid)
    if not e:
        e = ents[eid] = {"id": eid, "kind": kind, "key": key, "zone": zone,
                         "label": label or key, "ops": [], "agents": []}
    return e


def _extract(name, inp, cwd):
    """One tool call -> list of (kind, key, zone, label, op). Only what the call
    literally names; nothing speculative."""
    out = []
    if not isinstance(inp, dict):
        return out

    if name in READERS or name in WRITERS:
        p = _norm_path(inp.get("file_path") or inp.get("notebook_path"), cwd)
        if p:
            op = "read" if name in READERS else "write"
            out.append(("file", p, zone_of_path(p), _abbrev(p), op))
        return out

    if name == "WebFetch":
        u = inp.get("url") or ""
        h = urlparse(u).hostname
        if h:
            out.append(("host", h, zone_of_host(h), h, "fetch"))
        return out

    if name == "WebSearch":
        qq = (inp.get("query") or "").strip()
        if qq:
            out.append(("query", qq[:80], "internet", qq[:60], "search"))
        return out

    if name in ("Bash", "Monitor"):
        cmd = inp.get("command") or ""
        first = re.split(r"[\s|;&]+", cmd.strip())[0] if cmd.strip() else ""
        first = posixpath.basename(first)
        if first and re.fullmatch(r"[\w.+-]+", first):
            out.append(("command", first, "system", first, "execute"))
        for m in URL_RE.finditer(cmd):
            h = urlparse(m.group(0)).hostname
            if h:
                out.append(("host", h, zone_of_host(h), h, "fetch"))
        if NET_CMD.search(cmd):
            for m in HOST_ARG.finditer(cmd):
                h = m.group(1)
                if h and not h.endswith((".py", ".js", ".sh", ".md", ".json")):
                    out.append(("host", h, zone_of_host(h), h, "connect"))
        # files named on the command line, only when they look like real paths.
        # URLs are stripped first: "http://localhost:3000/api" is not a file.
        stripped = URL_RE.sub(" ", cmd)
        for m in PATH_RE.finditer(stripped):
            p = _norm_path(m.group(1), cwd)
            if p and len(p) > 4 and p != HOME and not p.startswith(("/dev", "/proc")):
                out.append(("file", p, zone_of_path(p), _abbrev(p), "touch"))
        return out

    if name.startswith("mcp__"):
        parts = name.split("__")
        svc = parts[1] if len(parts) > 2 else name
        out.append(("service", svc, "service", svc, "call"))
        return out

    return out


def analyze(graph, tool_events, cwd=None):
    """tool_events: {agent_label: [(tool_name, input_dict, ts), ...]}"""
    ents, edges = {}, {}
    for node in graph.get("nodes", []):
        aid = node["id"]
        for (name, inp, ts) in tool_events.get(aid, []):
            for (kind, key, zone, label, op) in _extract(name, inp, cwd):
                e = _entity(ents, kind, key, zone, label)
                e["ops"].append({"agent": aid, "op": op, "tool": name, "t": ts})
                if aid not in e["agents"]:
                    e["agents"].append(aid)
                ek = (aid, e["id"], op)
                edges.setdefault(ek, {"from": aid, "to": e["id"], "op": op, "count": 0})
                edges[ek]["count"] += 1

    # Derived relationships. Each needs two recorded operations to exist.
    findings, _seen = [], set()

    def _add(f):
        k = (f["kind"], f.get("entity"), f.get("from"), f.get("to"), f.get("agent"))
        if k in _seen:
            return
        _seen.add(k)
        findings.append(f)

    for e in ents.values():
        if e["kind"] != "file":
            continue
        writers = [o for o in e["ops"] if o["op"] == "write"]
        readers = [o for o in e["ops"] if o["op"] == "read"]

        # implicit dependency: written by one agent, read later by another
        for w in writers:
            for r in readers:
                if r["agent"] == w["agent"]:
                    continue
                if w.get("t") and r.get("t") and r["t"] < w["t"]:
                    continue                      # read happened first: no dependency
                declared = any(x["from"] == w["agent"] and x["to"] == r["agent"]
                               for x in graph.get("edges", []))
                _add({
                    "kind": "implicit-dep", "entity": e["id"], "label": e["label"],
                    "from": w["agent"], "to": r["agent"], "declared": declared,
                    "detail": f"{w['agent']} wrote {e['label']}, then {r['agent']} read it"
                              + ("" if declared else " — this dependency is not in the declared graph"),
                })

        # write contention: two different agents writing the same path
        wa = sorted({o["agent"] for o in writers})
        if len(wa) > 1:
            _add({
                "kind": "write-contention", "entity": e["id"], "label": e["label"],
                "agents": wa,
                "detail": f"{len(wa)} agents wrote {e['label']}: " + ", ".join(wa),
            })

    # external landing: an agent that pulled from the internet and wrote to disk
    for node in graph.get("nodes", []):
        aid = node["id"]
        touched = [e for e in ents.values() if aid in e["agents"]]
        remote = [e for e in touched if e["kind"] == "host" and e["zone"] == "internet"]
        wrote = [e for e in touched
                 if e["kind"] == "file" and any(o["agent"] == aid and o["op"] == "write" for o in e["ops"])]
        if remote and wrote:
            _add({
                "kind": "external-landing", "agent": aid,
                "hosts": [e["label"] for e in remote][:4],
                "files": [e["label"] for e in wrote][:4],
                "detail": f"{aid} fetched from {', '.join(e['label'] for e in remote[:2])}"
                          f" and wrote {', '.join(e['label'] for e in wrote[:2])}",
            })

    zones = {}
    for e in ents.values():
        zones[e["zone"]] = zones.get(e["zone"], 0) + 1

    for e in ents.values():
        e["opCount"] = len(e["ops"])
        e["ops"] = e["ops"][:40]

    return {
        "entities": sorted(ents.values(), key=lambda x: -x["opCount"]),
        "edges": list(edges.values()),
        "findings": findings,
        "zones": zones,
        "totals": {"entities": len(ents), "edges": len(edges), "findings": len(findings)},
    }
