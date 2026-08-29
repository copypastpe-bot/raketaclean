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
