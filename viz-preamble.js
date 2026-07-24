// ─────────────────────────────────────────────────────────────────────────
// wfviz preamble — drop this at the top of any Workflow script and the run
// renders in the live graph dashboard: labels, phases, effort, tools, and the
// exact edges, all recoverable in real time.
//
// Why a prompt header? A running workflow's only files that update live are the
// per-agent transcripts and journal.jsonl. The labeled graph (which node is
// which, and what depends on what) is not in either — so vnode() rides that
// identity into the agent's prompt as a ⟦wfnode {...}⟧ header the dashboard
// parses from the live transcript. It costs zero model tokens of orchestration
// and the subagent simply treats the first line as its task label.
//
// Usage — wrap agent() in vnode() and name each node's upstreams with `deps`:
//
//   phase('Search')
//   const hits = await parallel(SOURCES.map(s => () =>
//     vnode(s.prompt, { label:`search:${s.key}`, phase:'Search', deps:['scope'],
//                       model:'sonnet', effort:'low', tools:['Web','Read'],
//                       schema: ITEM, agentType:'general-purpose' })))
//
//   phase('Synthesize')
//   const report = await vnode(writePrompt, { label:'synth', phase:'Synthesize',
//     deps:['search:releases','search:blogs'], model:'opus', effort:'high' })
//
// `deps` are the labels of the nodes whose output feeds this one — they become
// the graph's edges. `tools`/`effort`/`label`/`phase`/`deps` are viz metadata;
// vnode strips the ones agent() doesn't accept before calling it.
// ─────────────────────────────────────────────────────────────────────────

// `role` gives a node a visual identity in the graph, so the course's topologies
// read at a glance instead of every node looking alike:
//   'router'   — classifies, then code picks the downstream edge   (step 8)
//   'verifier' — a gate a finding must survive to pass downstream  (step 9)
//   'reducer'  — dedupe/rank/merge over the whole upstream set     (step 6)
//   'judge'    — scores competing attempts, synthesises a winner   (step 9)
// `isolation:'worktree'` also marks the node ⧉ (step 10).
async function vnode(prompt, opts = {}) {
  const tag = {
    id: opts.label, phase: opts.phase,
    model: opts.model || 'inherit', effort: opts.effort || 'inherit',
    tools: opts.tools || null, deps: opts.deps || [],
    role: opts.role || null, isolation: opts.isolation || null,
  }
  const tagged = '⟦wfnode ' + JSON.stringify(tag) + '⟧\n' + prompt
  // tools/deps/role are viz-only; effort/model/label/phase/isolation are real agent() options
  const { tools, deps, role, ...agentOpts } = opts
  return agent(tagged, agentOpts)
}

// PREFERRED over vnode(). "The edge only exists when data actually moves across
// it." Declaring `deps` by hand lets the drawn edge drift from reality — you get
// a graph that looks connected while each agent runs blind. vnodeFrom derives
// the edges FROM the upstream results you pass, and injects those results into
// the prompt, so a declared edge and a real one cannot come apart.
//
//   const merged = await vnodeFrom({ 'probe:cost': cost, 'probe:latency': lat },
//     'Reconcile these two probes into one tradeoff statement.',
//     { label:'merge', phase:'Merge', schema: R, model:'haiku' })
//
// deps become ['probe:cost','probe:latency'] automatically and their text is in
// the prompt. Pass a node's result under the same label you gave that node.
async function vnodeFrom(upstream, prompt, opts = {}) {
  const deps = Object.keys(upstream || {})
  const body = deps.map((k) => {
    const v = upstream[k]
    const text = typeof v === 'string' ? v : JSON.stringify(v, null, 1)
    return `--- upstream · ${k} ---\n${text}`
  }).join('\n\n')
  const full = deps.length ? `${prompt}\n\nUse the upstream results below. Do not ask for them; they are here.\n\n${body}` : prompt
  return vnode(full, { ...opts, deps })
}
