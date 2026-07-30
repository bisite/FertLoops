# FertLoops

## Language conventions

**Spanish** — everything the team reads as prose:

- GitHub issue titles, bodies, and comments
- Markdown documentation under `docs/`, including ADRs and `CONTEXT.md`
- `README.md`

**English** — everything else, per standard software-engineering convention:

- Source code and identifiers
- Code comments and docstrings
- Config files (including this file and `docs/agents/*.md`, which are agent config, not team documentation)
- Git commit messages, branch names, PR titles
- Names of architecture components and services

Inside Spanish prose, keep code identifiers in their English form — write "el servicio `IrrigationScheduler`", not a translated name — so documentation stays linkable to the code.

## Agent skills

### Issue tracker

Issues live as GitHub issues in `bisite/FertLoops`, managed via the `gh` CLI. Issue content is written in Spanish. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles, each label string equal to its name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` and `docs/adr/` at the repo root, written in Spanish. See `docs/agents/domain.md`.
