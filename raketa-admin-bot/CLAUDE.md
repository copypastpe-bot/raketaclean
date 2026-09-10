# CLAUDE

Read this before working in the project.

## Goal

Owner-only admin bot for the cleaning company (raketaclean): a separate service on the VPS that automates administrative work the owner does by hand. Feature v1: automatic amoCRM deal processing driven by the worker bot's database (design approved 2026-08-24). Future features: calendar sync, partner Excel processing, amo→bot import replacement.

## Read Order

Infra map (canonical, token-light): /Users/evgenijpastusenko/Projects/agent1/docs/INFRA_MAP_LITE.yaml
Project context (central memory): /Users/evgenijpastusenko/Projects/agent1/project_ai_context/raketa-admin-bot/AGENT_STATE.md
Project log (central memory): /Users/evgenijpastusenko/Projects/agent1/project_ai_context/raketa-admin-bot/SESSION_LOG.md

1. central `AGENT_STATE.md` in `project_ai_context/raketa-admin-bot/`
2. recent entries in central `SESSION_LOG.md` in `project_ai_context/raketa-admin-bot/`
3. `docs/plans/2026-08-24-amo-sync-design.md` (approved design)
4. current implementation plan in `docs/plans/`
5. recon facts: `/Users/evgenijpastusenko/Projects/tgbot-v1/recon/` (00-summary, 06-matching-metrics)

## Related Projects

`smm-autopost` (`~/Projects/smm-autopost`) — third project, created 2026-08-29: auto-posting
of media from cleaning jobs to social networks. Runs on a different server (Contabo), shares
no data with either bot. Context: `agent1/project_ai_context/smm-autopost/`.

`tgbot-v1` (`~/Projects/tgbot-v1`) is the **main project**: the working bot that owns the
shared database, talks to clients through Wahelp, and now runs the amoCRM exchange and the
pre-job client conversation. Its context lives at
`/Users/evgenijpastusenko/Projects/agent1/project_ai_context/tgbot-v1/`.

**Start the session in the directory of the project you are about to change.** The working
directory decides which project rules load, where git commands land, and which context
files get updated at session close. Most work happens in `tgbot-v1`; this repository is
opened when the admin bot itself is being built. Do not merge the two memories: this
project's hard rule — never write to the worker bot's tables — has no counterpart there,
and mixing the two will eventually put a rule where it does not belong.

## Central Context

This project uses central agent memory outside the current repository.
If `./AGENT_STATE.md` or `./SESSION_LOG.md` are missing here, that is expected.
Read and update only these registered files:

- State: /Users/evgenijpastusenko/Projects/agent1/project_ai_context/raketa-admin-bot/AGENT_STATE.md
- Log: /Users/evgenijpastusenko/Projects/agent1/project_ai_context/raketa-admin-bot/SESSION_LOG.md

## Key Sources

- `docs/plans/2026-08-24-amo-sync-design.md` — approved v1 design, owner decisions 1-10
- `/Users/evgenijpastusenko/Projects/tgbot-v1/recon/` — recon reports (amo structure, matching metrics, processes)
- `/Users/evgenijpastusenko/Projects/tgbot-v1/notifications/amocrm_api.py` — reference amo API client to port
- `/Users/evgenijpastusenko/Projects/tgbot-v1/bot.py:3251` — canonical phone normalization to port
- `/Users/evgenijpastusenko/Projects/tgbot-v1/docs/analytics_deploy.md` — deploy pattern to follow

## Hard Rules (project-specific)

- NEVER write to the worker bot's tables (schema `public` of the shared DB): read-only. All own state lives in a dedicated schema.
- All amoCRM writes go through the dedicated write-scoped integration, never through the worker bot's read-only token.
- Every feature has its own kill switch; default off until owner enables.
- PII (client phones, names, addresses) never goes into git; mask phones to last 4 digits **in logs** (`adminbot.phone.mask`).
- Messages to the owner show the **full phone and the order date** (`adminbot.phone.for_owner`): the bot is private, he is its only recipient, and he needs to reach the client without opening CRM. Owner's decision 2026-08-26 — do not "restore" masking there.

## Working Rules

- Prefer real code and config over documentation when facts conflict.
- Keep changes aligned with the current project direction in `AGENT_STATE.md`.
- Record uncertainty explicitly instead of guessing.
- If required facts are missing, ask the user directly instead of speculative exploration.
- Use detective mode only when the user explicitly asks to find a solution or process.
- Central context for this project lives in `agent1/project_ai_context/`, not in the current repo.
- Missing local `AGENT_STATE.md` / `SESSION_LOG.md` in this repo is normal for `context_mode: central`.
- Read and update only the registered central paths above.

## Model Routing

Hard rule set by the owner on 2026-09-06. Sessions open on Fable by default unless the owner picks another model.

First line of every reply to a new task: the task type and how it will be run, in the owner's language.

- Discussion, architecture, solution search, brainstorming, writing a spec or a plan: Fable, in the current session.
- Writing code, executing a spec, implementation: Opus.
- The simplest operations: Sonnet.
- Hard bugs, non-standard situations, heavy analysis: stop and discuss the model choice with the owner before starting.

Execution rules:

- Small edits (a few lines, one file, no design decisions): Fable does them itself in the current session.
- Large edits or a feature: Fable writes the spec to `docs/plans/<YYYY-MM-DD>-<name>.md` (create the folder if missing), then asks the owner how to run it: subagents from this session, or a new session on Opus with the spec. The owner decides; never pick for them.
- Fable does not write code from a finished spec. The only exception is the owner saying "do it here".
- Subagents run on Opus (implementation) or Sonnet (mechanical work). Never spawn a Fable subagent unless the owner explicitly asks for one: it burns the usage limit for nothing.
- A subagent has no memory of this conversation: hand it the spec file, not a retelling.
- The executor verifies its own result and records it in the session log. Fable reviews the diff only when the owner asks.

## Session and Memory Rules

Hard rule set by the owner on 2026-09-10. Full text of the rules: `~/Projects/agent1/docs/plans/2026-09-10-working-rules.md`.

Memory:

- Read only the parts of a file you need. Every file longer than 500 lines has a function map next to it (`<name>.map.md`): the list of functions with one line of purpose each. The agent that changed the file updates the map.
- Trim command output to the useful part (last lines, filter by pattern) instead of printing it whole.
- Screenshots and images are not forwarded to executors. The coordinator describes in words what is on them.
- The coordinator stores subagent reports in a log file and keeps a summary of at most 20 lines in its own memory.
- Above 400k tokens of memory the coordinator runs `/compact` or opens a new session from the spec and the log. The same after a break on the usage limit.
- Do not copy the full task text into a subagent prompt: give the path to the plan and the task number.
- One working session equals one task: no longer than one day and 300 steps, then close it by the rules and open a new one. Auto-compaction does not fire on 1M-token windows, so sessions are closed by hand.

Fixes after review:

- The list of review findings is filtered first (what nobody asked for is not done) and written to a file.
- A fresh agent applies the fixes if the previous one is above 300k tokens of memory or has already done one round. Superpowers 6.3 returns the same executor for up to three rounds by default; our rule is stricter and the coordinator says so in the spec.
- No more than two rounds per task; after that the owner decides.

Review:

- A spec-compliance review on every task: short, catches drift immediately.
- A quality review not on every task but per block of 3-4 related tasks, plus one review of the whole branch at the end.
- A review only reads and writes findings to a file. It does not fix code.

Environment and stand:

- Before a spec is written, a short reconnaissance task on the stand: build, start, test run, known traps. The result goes to `AGENT_STATE.md`.
- Any external process (1C, docker, tests) starts with a hard timeout and is killed when it expires. The timeout lives in a project script, not in the agent's head.
- Every project keeps a `.claude/settings.json` with the permissions its own scripts need (skill `fewer-permission-prompts`). The same permission denial must not repeat eight times.

Models:

- The split in Model Routing stands. Refinement: mechanical tasks (scripts, documents, edits from a ready list in one or two files) go to Sonnet, integration and debugging to Opus, design to Fable.
- Review is routed by diff size: a small mechanical diff does not need Opus.

Rules for `agent1`:

- both `CLAUDE.md` templates and every active project's `CLAUDE.md` carry this section verbatim (in Russian where the project file is written in Russian), the same as Model Routing.

## Project Rules (2026-09-10)

Common rules for python projects (Rules 4.1):

- One session equals one task. A working session does not live longer than one day and 300 steps. A new task opens a new session from `AGENT_STATE.md` and the log. This also covers Opus sessions where code is written directly in the conversation.
- At 300k tokens of memory the coordinator or the executor runs `/compact` stating what to keep, or closes the session by the closing rules.
- Tests during work are targeted: only the affected file or a selection (`pytest tests/x.py -q --tb=short`), output through `tail`. A full run once before commit and once before deploy. TDD stays: this project has a standard `pytest`.
- `ssh` only through the project's runbook scripts, output trimmed to the useful part. No manual step-by-step diagnosis on the server from the main session: write a script and run it once.
- Files longer than 500 lines are read in parts; the function map lives next to the file (owner decision 2026-09-10).
- `.claude/settings.json` of this project holds the permissions its own work needs: `pytest`, `git status/diff/log`, file reads (`cat`, `sed -n`, `grep`, `rg`), `python -m`, project scripts. `ssh`, `scp`, `rsync` stay a question for the owner.
- Subagent worktrees are removed after merge (`git worktree prune` plus branch deletion). No leftovers between sessions.
- Images and screenshots do not go into working-session memory, unless the task is about the interface and the owner chose to show the screen.

This project (Rules 4.2):

- The two-step deploy rule is already recorded in the state file; keep following it.
- Add a `.claude/settings.json` with the project's own permissions.
- Keep the function map for `db.py`.

Files longer than 500 lines and their function maps:

- `adminbot/db.py` (1200 lines) -> `adminbot/db.py.map.md`

## Skill Usage

- Do not load or invoke Superpowers or other optional skills automatically at session start.
- Before using any Superpowers skill, ask the user for permission and name the exact skill plus the reason.
- For routine tasks such as checking databases, reading logs, inspecting git status, reviewing files, or running documented deploy/diagnostic commands, use project docs and direct commands first; do not read skills unless the user approves.
- If the user explicitly asks to use a skill or plugin, use only the minimum relevant skill files and state that you are doing so.

## Git Hygiene

- Run `git status --short` before editing, before committing, and before deploy.
- Commit completed logical steps in small, focused commits.
- Do not mix unrelated changes into one commit.
- Do not deploy from a dirty worktree.
- Do not leave completed work uncommitted at session close; do not leave finished commits unpushed without saying so.

## Deploy Rules

- Target: same VPS as the worker bot (`admin@91.200.150.68`), separate systemd service, pattern of `tgbot-v1/docs/analytics_deploy.md`.
- Deploy runbook must be written in `docs/` BEFORE the first deploy; until then, do not improvise a deploy.
- Prefer commit -> push -> deploy; standard path `local -> git -> VPS`.
- Do not search for passwords, invent credentials, or guess access methods.
- If a step needs `sudo` beyond documented NOPASSWD commands, stop and ask the user.

## Context File Rules

### AGENT_STATE.md
- Rewrite fully at each session close. Maximum 60 lines.
- Allowed: project/status header, Purpose, Current State (branch + HEAD + deploy status), Pending, Known Limitations.
- Forbidden: Verified sections with commands or outputs, deploy receipts, local machine tooling unrelated to this project.

### SESSION_LOG.md
- Add entries at the top (newest first). Keep the last 10 entries.
- Move entries beyond 10 to `SESSION_LOG_archive.md` in the same directory (append, never delete).
- Each entry: maximum 25 lines — date/title, status, scope, key changes (bullets), deploy SHA if applicable, notes.
- Forbidden: command outputs, curl responses, container statuses, full test or docker output.

## End Of Session Requirements

Before ending the session:
1. run `git status --short`;
2. rewrite `/Users/evgenijpastusenko/Projects/agent1/project_ai_context/raketa-admin-bot/AGENT_STATE.md`;
3. add one new entry at the top of `/Users/evgenijpastusenko/Projects/agent1/project_ai_context/raketa-admin-bot/SESSION_LOG.md`;
4. apply Context File Rules above to both files;
5. run `git status --short` again and commit completed work in one or more small logical commits;
6. state clearly whether the work was pushed to the remote or is still local only.

## Current Focus

Turn the approved design into an implementation plan (`docs/plans/`), then build v1 (amo_sync) step by step: skeleton → matcher + history exam → amo write client → Telegram UI → rehearsal → backlog run from 2026-08-21.
