# graph-claude — a living graph for Claude Code dynamic workflows

Run a **dynamic workflow** in Claude Code (say "workflow" in a prompt, run
`/deep-research`, or turn on ultracode) and Claude writes a JS orchestration
script and spawns a fleet of subagents. wfviz turns that fleet into a graph you
watch in real time: one node per subagent with its **model, effort, tools,
tokens and state**, edges for data flow, phase columns — and **click any node to
read its actual transcript**: prompt, thinking, tool calls, results, timings.
Nothing is mocked; every field is read from the running workflow's own files.

- **Graph** — [live-graph.png](docs/graph.png): scope→search→reduce→verify→synthesize, tiers colour-coded, exact `deps` edges.
- **Hover** — a frosted tooltip glowing in that node's model-tier colour, streaming its live trace (tool calls, arguments, outputs, timings), refreshed every 900ms while the agent runs. `?hover=<label>` deep-links it.
- **Drill-down** — click a node → its real result, prompt, and event-by-event transcript, streaming while it runs.

> Note: Claude's extended thinking is persisted encrypted — transcripts contain
> `{"type":"thinking","thinking":"","signature":"…"}` — so no plaintext
> chain-of-thought exists locally to stream. The tooltip shows the *observable*
> trace (what the agent did), and renders a "reasoning" section only if
> non-empty thinking text is ever present.
- **Shareable replay** — `gen_replay.py` bakes a finished run (lifecycle + real transcripts) into one self-contained HTML page.

## Run it

```bash
./wfviz                      # start server + open browser (idempotent)
# or
python3 wfviz.py --port 8777 # then open http://127.0.0.1:8777
```

It auto-discovers the newest workflow across all your sessions and follows it
live. Scroll to zoom, drag to pan, click a node, double-click to re-fit.
`?node=<label>` deep-links straight to a node's transcript.

## How it stays truly live (the part that matters)

A completed workflow leaves a tidy `tasks/<id>.output` and `workflows/<id>.json`
— but both are written **only at completion**; during a run they're absent or
mid-write. Polling them shows nothing, then snaps to done. So wfviz reads the
files that actually update in real time:

| source | gives | live? |
|---|---|---|
| `subagents/workflows/wf_<id>/journal.jsonl` | per-agent started/result + result value | ✅ line-atomic, live |
| `subagents/workflows/wf_<id>/agent-<id>.jsonl` | the agent's full transcript (prompt, thinking, tool calls, timings, tokens) | ✅ grows live |
| `subagents/workflows/wf_<id>/agent-<id>.meta.json` | model, agentType | ✅ at spawn |
| `workflows/wf_<id>.json` | final tokens/labels/edges | ⛔ completion only → used as the "done" signal + enrichment |

The one thing those live files lack is the *labels and edges* — which node is
which, and what feeds what. That identity rides into each agent's prompt as a
`⟦wfnode {...}⟧` header (see below), so the labeled graph and its edges are
recoverable live from the transcripts alone. Without the header you still get a
real graph, keyed by agent id + prompt preview and laid out by start-time
concurrency.

## Make any workflow render richly (the standard)

Paste the preamble from [`viz-preamble.js`](viz-preamble.js) at the top of a
workflow script, wrap `agent()` in `vnode()`, and name each node's upstreams
with `deps`:

```js
phase('Search')
const hits = await parallel(SOURCES.map(s => () =>
  vnode(s.prompt, { label:`search:${s.key}`, phase:'Search', deps:['scope'],
                    model:'sonnet', effort:'low', tools:['Web','Read'],
                    schema: ITEM, agentType:'general-purpose' })))

phase('Synthesize')
const report = await vnode(writePrompt, { label:'synth', phase:'Synthesize',
  deps:['search:releases','search:blogs'], model:'opus', effort:'high' })
```

`deps` are the labels whose output feeds this node — they become the edges.
`vnode()` prepends the `⟦wfnode⟧` header, strips the viz-only `tools`/`deps`
keys, and calls `agent()` normally. It costs zero orchestration tokens.

## Terminal mirror — every session mirrors *itself*

`wfviz-term` opens the graph with a **live, controllable Claude terminal**
floating over it: a `ttyd` web terminal attached to a `tmux` session. tmux allows
many clients on one session, so the browser terminal *mirrors and controls* the
same session — drive Claude Code from the browser while the graph animates.

Launch each Claude Code session with `ccm` so it lives in its own tmux session:

```bash
./ccm          # tmux session named after the current directory
./ccm mywork   # or name it yourself; re-run to re-attach
```

From then on, a `PreToolUse` hook on the `Workflow` tool (in `~/.claude/settings.json`,
pointing at this repo's `wfviz-term`) runs `wfviz-term` automatically, so **asking for a
workflow pops open the browser showing the session you asked from**:

- it reads `$TMUX` to learn **which tmux session fired it**, and mirrors that one
- each session gets **its own ttyd port** (registry in `~/.wfviz/`), so several
  sessions can have their own mirror open simultaneously
- it reads the hook's JSON payload for the Claude `session_id` and scopes the
  graph with `?session=<id>`, so you see **that session's** workflow runs

Remove the hook entry to disable. `?termport=<port>` picks a mirror manually.

**If the session isn't tmux-hosted** it still follows *that* session — never some
other one. A process can't hand its live TTY to tmux after the fact (macOS blocks
`reptyr`-style reparenting), so instead the window shows a **read-only live feed**
of the session's own transcript (`~/.claude/projects/*/<session-id>.jsonl`,
tailed via `/session?id=`): your prompts, Claude's replies and every tool call,
refreshed every 2s. Start the session with `ccm` to get a terminal you can type in.

## Colour legend

**Opus** amber · **Sonnet** cyan · **Haiku** mint · **Fable** violet (model tier, node accent) ·
**rust→amber** edge = fan-out · **teal→green** edge = merge ·
dashed ghost = planned · glowing sheen = running · ✓ = done.

## Files

- `wfviz.py` — live server: reads journal + transcripts + meta, serves `/state`, `/agent`, `/runs`
- `dashboard.html` — the animated graph + transcript drawer (works live or in replay)
- `viz-preamble.js` — the copy-paste convention for labels/edges/effort/tools
- `gen_replay.py` — bake a finished run into a shareable replay page
- `wfviz` — launcher (graph only)
- `wfviz-term` — launcher with the tmux/ttyd terminal mirror panel
