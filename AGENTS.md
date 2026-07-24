# Working agreement

Two agents are working in this repo concurrently. To avoid clobbering each
other, each owns a set of files. Touch your own; leave the others alone.

## Ownership

| Area | Files | Owner |
|---|---|---|
| Docs, demo reel | `README.md`, `demo/record.py`, `docs/*` | agent A |
| Security overlay | `security.py`, `docs/SECURITY-DESIGN.md` | agent B |
| Core server | `wfviz.py` | shared — see rules |
| Dashboard UI | `dashboard.html` | shared — see rules |

## Rules for the two shared files

`wfviz.py` and `dashboard.html` are the only files both of us need. To keep
merges trivial:

1. **Additive only.** Add new functions/blocks; don't reflow or reformat
   existing code you didn't write. A reformat turns a 3-line diff into a 300-line
   conflict.
2. **Security code is namespaced.** All security analysis lives in
   `security.py`. `wfviz.py` integrates it through a single import and one call
   site, so the shared-file footprint is a few lines rather than a subsystem.
3. **Dashboard additions are self-contained blocks**, fenced by comments:
   `/* === security overlay: start === */` … `/* === security overlay: end === */`
   Everything for a feature stays inside its fence.
4. **Commit your own paths.** `git add <your files>` — never `git add -A`, which
   sweeps up the other agent's in-progress work.
5. **Check before you write.** `git status` first; if a file you need is already
   modified and isn't yours, leave it and note it here.

## In progress

- **agent A** — README rewrite, demo recording (`demo/record.py`, `docs/demo.mp4`).
  Uncommitted at time of writing; left untouched deliberately.
- **agent B** — security overlay: capability surfacing, taint reachability,
  verifier-gate lint. Design in `docs/SECURITY-DESIGN.md`.
