# Security Policy — Project Autopilot

## Security Model

Project Autopilot is a human-supervised coding control plane. It constrains Hermes workers through project-scoped state, fixed leases, durable Kanban task bindings, isolated Git worktrees, exact verification profiles, per-tool hooks, independent evidence, and explicit human gates.

It is **not** an operating-system or container sandbox for hostile project code. An explicitly approved verification command such as `python -m pytest` executes project-owned code with the permissions of the Hermes process. Configure only commands and repositories you trust to run locally. Use an external container/VM sandbox when evaluating untrusted code.

## Live-Execution Guarantees

### Authorization and lifecycle

- A live worker requires a canonical project registration, explicitly approved brief, unexpired fixed `autonomous-development` lease, exact verification profile, matching Hermes Project, running gateway, and inactive kill switch.
- Development and verifier cards are created blocked. Their project/loop/lease/task/workspace bindings are persisted before Development or remediation is promoted.
- The pre-tool hook reloads the kill switch, lease, loop status, task role, and worktree binding for every Autopilot worker tool call.
- Expired, canceled, stopped, replaced-lease, unbound, or cross-task operations fail closed. A worker may still block its own card to report why it stopped.
- Kanban max runtime provides a second hard timeout when a worker makes no additional tool calls.

### Workspace and tool confinement

- Development runs in a task-specific Git worktree. Verifier tasks are read-only and inspect the same worktree.
- File tools resolve canonical paths and reject path/symlink escape from the bound worktree.
- Autopilot workers may address only their own bound Kanban task and board.
- Unknown/external tools, delegation, cron, vision/local-image access, artifact uploads, background terminal execution, shell operators, destructive Git, dependency installation, commit, push, merge, deployment, migration, credentials, and personal-account flows are denied.
- Non-Git terminal commands must exactly match one configured verification argv and declared cwd. Git is limited to read-only subcommands and rejects repository/worktree overrides, output files, external helpers, path escape, and pager execution.
- Completion metadata is bounded and inspected before Kanban persistence; recognized secrets or personal identifiers must be redacted first.

### Verification and remediation

- Verifier evidence is bound to project, loop, brief, board, task, run, starting revision, verification-profile digest, complete changed paths, and every exact configured check.
- Malformed, missing, duplicate, extra, mismatched, or non-zero required evidence cannot be accepted.
- Verification failure can create only the configured bounded number of remediation/re-verification cycles. Exhaustion or ambiguity moves the loop to `NEEDS_HUMAN`.
- Passing evidence moves only to `AWAITING_HUMAN_ACCEPTANCE`; it is never auto-accepted.

### Checkpoint and optional commit

- Human acceptance does not commit.
- Checkpoint creation, one-time commit authorization, and local commit are three separate explicit commands.
- Checkpoints reject unsafe paths, symlinks, oversized payloads, obvious secrets, changed-path mismatches, and concurrent source-workspace changes.
- Authorization and commit revalidate every checkpoint archive member, per-file size/hash, canonical content digest, live worktree bytes, source status, provenance, expiry, and one-time authorization state.
- The only commit operation targets the isolated worktree. There is no Autopilot push, merge, deploy, migration, or release command.

## Original Workspace Preservation

Autopilot records the original workspace Git-status digest before dispatch and compares it again before checkpoint, authorization, and commit. It never runs its own write or Git mutation commands in the registered source workspace. Existing dirty files are not copied into the worker worktree.

Because approved verification commands execute local project code, strict protection against malicious child-process behavior requires an external OS/container sandbox. The status-digest checks detect ordinary concurrent or accidental source-tree changes; they are not a substitute for kernel-level isolation.

## Prohibited Implementation Patterns

The source security scan rejects:

- `shell=True`
- `os.system()`
- dynamic `eval()` / `exec()`
- pickle deserialization
- `threading.Thread` and `os.fork()`
- direct production SQLite writes
- obvious embedded credentials or personal identifiers

Host commands are dispatched through the public Hermes tool API using structured argv construction. Autopilot does not write Hermes session or Kanban SQLite databases directly.

## Secret and PII Handling

- Audit and durable verifier excerpts are bounded and redacted.
- Worker prompts forbid reading or reproducing credentials and personal identifiers.
- Raw completion metadata containing recognized secret/PII patterns is blocked before persistence.
- Test fixtures use synthetic identifiers only.

No regex-based detector is perfect. Do not place credentials in prompts, briefs, source files, verification output, or Autopilot metadata.

## Reporting

Report suspected vulnerabilities privately to the project maintainers. Do not include credentials, personal data, or exploit payloads in public issues.
