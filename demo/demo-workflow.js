export const meta = {
  name: 'wfviz demo · honest edges',
  description: 'Reference demo — every edge carries real data: scope → 4 probes → merge → 2 checks → wrap',
  phases: [{ title: 'Scope' }, { title: 'Probe' }, { title: 'Merge' }, { title: 'Check' }, { title: 'Wrap' }],
}

// ── wfviz preamble (see ../viz-preamble.js) ──
async function vnode(prompt, opts = {}) {
  const tag = { id: opts.label, phase: opts.phase, model: opts.model || 'inherit',
                effort: opts.effort || 'inherit', tools: opts.tools || null, deps: opts.deps || [] }
  const { tools, deps, ...a } = opts
  return agent('⟦wfnode ' + JSON.stringify(tag) + '⟧\n' + prompt, a)
}
// Edges derived from the data actually passed — they cannot drift from reality.
async function vnodeFrom(upstream, prompt, opts = {}) {
  const deps = Object.keys(upstream || {})
  const body = deps.map((k) => {
    const v = upstream[k]
    return `--- upstream · ${k} ---\n${typeof v === 'string' ? v : JSON.stringify(v, null, 1)}`
  }).join('\n\n')
  const full = deps.length
    ? `${prompt}\n\nUse the upstream results below. Do not ask for them; they are here.\n\n${body}`
    : prompt
  return vnode(full, { ...opts, deps })
}

const R = { type: 'object', additionalProperties: false,
            properties: { finding: { type: 'string' } }, required: ['finding'] }
const txt = (r) => (r && r.finding) || ''

phase('Scope')
const scope = await vnode(
  'Name exactly four distinct angles for judging whether to fan agents out instead of chaining them. ' +
  'Number them 1-4, one line each. Put it in finding.',
  { label: 'scope', phase: 'Scope', schema: R, model: 'haiku', effort: 'low',
    agentType: 'general-purpose', tools: ['Read', 'Web'], deps: [] })

phase('Probe')
const ANGLES = ['latency', 'cost', 'reliability', 'debuggability']
const probes = await parallel(ANGLES.map((k) => () =>
  vnodeFrom({ scope: txt(scope) },
    `Take the scoping list above. Expand ONLY the "${k}" angle in ~60 words, with one concrete example. Put it in finding.`,
    { label: `probe:${k}`, phase: 'Probe', schema: R, model: 'haiku', effort: 'low',
      agentType: 'general-purpose', tools: ['Read', 'Grep', 'Web'] })))

phase('Merge')
const merged = await vnodeFrom(
  Object.fromEntries(ANGLES.map((k, i) => [`probe:${k}`, txt(probes[i])])),
  'Reconcile these four probes into ONE tradeoff statement of ~70 words. Quote the specific probe ' +
  'that most constrains the decision, by name. Put it in finding.',
  { label: 'merge', phase: 'Merge', schema: R, model: 'haiku', effort: 'low', tools: ['Read'] })

phase('Check')
const CHECKS = [['sound', 'Is the statement logically sound? Name the weakest claim in it.'],
                ['gap',   'What does the statement omit that would change the decision?']]
const checks = await parallel(CHECKS.map(([k, q]) => () =>
  vnodeFrom({ merge: txt(merged) },
    `${q} Answer in ~45 words, referring to the statement's actual wording. Put it in finding.`,
    { label: `check:${k}`, phase: 'Check', schema: R, model: 'haiku', effort: 'low',
      agentType: 'general-purpose', tools: ['Read'] })))

phase('Wrap')
const wrap = await vnodeFrom(
  { merge: txt(merged), 'check:sound': txt(checks[0]), 'check:gap': txt(checks[1]) },
  'Write the final guidance in ~80 words on when to reach for a fanned-out agent graph over a linear ' +
  'chain. Incorporate both critiques above. Put it in finding.',
  { label: 'wrap', phase: 'Wrap', schema: R, model: 'haiku', effort: 'low', tools: ['Read', 'Write'] })

return { scope, probes: probes.filter(Boolean), merged, checks: checks.filter(Boolean), wrap }
