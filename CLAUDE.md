@AGENTS.md

# Claude Code

Use the imported AGENTS.md as the shared engineering and authority policy. Keep this file limited
to Claude Code behavior.

- **Never stage, commit, or push private keys or signing seeds under any circumstances.** Review
  the staged paths and secret scan before committing. If suspected signing material is present,
  stop and report its location without revealing the value.
- Consult [path-scoped rules](.claude/rules/) for the files in scope. Use the linked charter,
  runbook, and evidence pages on demand. Keep changing status in the evidence index.
- Use `/memory` to confirm the project instructions are loaded if behavior differs across
  sessions. Personal preferences belong in local memory and durable team rules belong in Git.
- During `/compact` or a handoff, retain the exact changed paths, commit, authority scope, active
  safety constraints, test exit codes, evidence references, open findings, and next safe action.
- Keep imported context small. Add durable shared rules to AGENTS.md, file-specific conventions
  to `.claude/rules/`, and operational details to the relevant runbook.

See [Claude Code project instructions](https://code.claude.com/docs/en/memory).
