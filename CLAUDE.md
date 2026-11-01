## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
- A committed graphify-out/graph.json is rewritten whole on every build, so a large diff to it in a pull request is expected. Do not flag it as churn or suggest skipping the graph rebuild or its hooks.

## How to use the graph here (project rules)

- Start broad questions ("how does X fit together", "what touches Y", "where does Z live") with `graphify query "<question>"`, or `graphify explain "<symbol>"` / `graphify path "<A>" "<B>"`. Keep questions narrow: a query returns a ~2k-token subgraph and truncates the rest.
- A query points to nodes and files; it is not the answer. Read the specific functions it names (targeted reads, not whole files) before changing anything.
- For a precise edit where you already know the file and symbol, go straight to a targeted grep or read. The graph is for orientation, not for every lookup.
- Hooks keep it current: a refresh at session start, after each turn that changed source files, and after every commit. Run `graphify update .` yourself if you edited code in a way the hooks did not catch.
- `graphify-out/` is git-ignored tool output. Never commit it, and never run a history rewrite or checkout that could delete it.
- Architecture, APIs and every pathway are in `DESIGN.md`; the graph is for finding code, `DESIGN.md` is for understanding it.

## Commits, privacy and the second computer (project rules)

- The repo is PUBLIC. Every commit, from any computer, must use the no-reply identity: `Siddharth <38179155+siddh30@users.noreply.github.com>` (author AND committer). No personal email, no hostnames, no IP addresses, no usernames in paths.
- Every computer that commits (the MacBook included) must set it once in this repo: `git config user.name "Siddharth"` and `git config user.email "38179155+siddh30@users.noreply.github.com"`. Check `git config user.email` before the first commit on a computer.
- Before pushing, run `git log origin/<branch>..HEAD --format='%ae|%ce'`: anything that is not the no-reply address must be fixed first (`git commit --amend --reset-author`, or a rebase for older commits). If it is already pushed, fixing it means a force-push: ask the user first, use `--force-with-lease`, then tell the other computer to `git fetch && git reset --hard origin/<branch>`.
- Per-computer settings live in `config.local.yaml` and `.env`, never in the repo. A worker's name for discovery is `SWARM_WORKER_NAME` in its `.env`; the controller's `host` for that computer must be the same name, not an IP address (only a name is looked up again when the address changes).
