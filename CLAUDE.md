# CLAUDE

Общие правила живут в глобальном `~/.claude/CLAUDE.md` и грузятся программой сами. Здесь только то, что касается этого проекта.

Read this before working in the repository.

## Layout

One repository, two bots, two systemd services on the same VPS.

- Repository root — the **worker bot** (`bot.py`): orders, loyalty, payroll, cashflow,
  notifications, admin flows, the amoCRM exchange and client messaging.
  Service `telegram-bot.service`, deployed to `/opt/telegram-bot`.
- `raketa-admin-bot/` — the **admin bot**, owner-only, its own code and its own tests.
  Service `raketa-admin-bot.service`, deployed to `/opt/raketa-admin-bot/app`.

The two share the database (`clients_db`) and the amoCRM account, nothing else.
Rules in this root file are common to the whole repository; `raketa-admin-bot/CLAUDE.md`
holds the admin bot's own rules. **Read the `CLAUDE.md` of the folder you are changing.**

## Goal

Maintain the internal cleaning-company Telegram bot without destabilizing the currently working order, loyalty, payroll, cashflow, notification, and admin flows. The next strategic direction is Google Calendar integration.

## Read Order

Infra map (canonical, token-light): /Users/evgenijpastusenko/Projects/agent1/docs/INFRA_MAP_LITE.yaml

Always, whichever bot you touch:

1. `./AGENT_STATE.md`
2. recent entries in `./SESSION_LOG.md`

Worker bot (repository root):

3. `project.md`
4. `technical_task_google_calendar_integration.md`
5. `README.md`
6. `bot.py`
7. `docs/notification_rules.json`
8. `notifications/`
9. `crm/wahelp_service.py`

Admin bot (`raketa-admin-bot/`):

3. `raketa-admin-bot/CLAUDE.md`
4. `raketa-admin-bot/docs/plans/2026-08-24-amo-sync-design.md` (approved design)
5. current implementation plan in `raketa-admin-bot/docs/plans/`
6. recon facts: `docs/recon/` (00-summary, 06-matching-metrics)

## Related Projects

`smm-autopost` (`~/Projects/smm-autopost`) — third project, created 2026-08-29: auto-posting
of media from cleaning jobs to social networks. Different server (Contabo), no shared data
with these bots. Context: `agent1/project_ai_context/smm-autopost/`.

## Local Context

This project uses `context_mode: local` in `/Users/evgenijpastusenko/Projects/agent1/registry.yaml`.
Agent memory lives inside this repository, one state and one log for both bots:

- State: `./AGENT_STATE.md`
- Log: `./SESSION_LOG.md`
- Archive: `./SESSION_LOG_archive.md`

Do not create or update copies of these files under
`/Users/evgenijpastusenko/Projects/agent1/project_ai_context/` — for this project
the local files are the registered ones.

## Key Sources

Worker bot (root):

- `bot.py`
- `project.md`
- `README.md`
- `notifications/`
- `docs/notification_rules.json`
- `crm/`
- `app/migrations/`
- `scripts/`
- `technical_task_google_calendar_integration.md`

Admin bot (`raketa-admin-bot/`):

- `raketa-admin-bot/docs/plans/2026-08-24-amo-sync-design.md` — approved v1 design, owner decisions 1-10
- `docs/recon/` — recon reports (amo structure, matching metrics, processes)
- `notifications/amocrm_api.py` — reference amo API client to port
- `bot.py`, `normalize_phone_for_db()` — canonical phone normalization to port
- `docs/analytics_deploy.md` — deploy pattern to follow

## Hard Rules (project-specific)

These bind the admin bot (`raketa-admin-bot/`):

- NEVER write to the worker bot's tables (schema `public` of the shared DB): read-only. All own state lives in a dedicated schema.
- All amoCRM writes go through the dedicated write-scoped integration, never through the worker bot's read-only token.
- Every feature has its own kill switch; default off until owner enables.
- PII (client phones, names, addresses) never goes into git; mask phones to last 4 digits **in logs** (`adminbot.phone.mask`).
- Messages to the owner show the **full phone and the order date** (`adminbot.phone.for_owner`): the bot is private, he is its only recipient, and he needs to reach the client without opening CRM. Owner's decision 2026-08-26 — do not "restore" masking there.

The first rule is also enforced technically: the `adminbot` Postgres role has `SELECT` only
on schema `public` (since 2026-08-26).

## Working Rules

- Project context state lives in `./AGENT_STATE.md` inside this repository.
- Project session log lives in `./SESSION_LOG.md` inside this repository.
- If required facts are missing, ask the user directly.
- Do not enumerate speculative options by default.
- Use detective mode only when the user explicitly asks to find a solution or process.
- Prefer real code and config over documentation when facts conflict.
- Keep changes aligned with the current project direction in AGENT_STATE.md.
- Record uncertainty explicitly instead of guessing.

- Treat `bot.py` as the main runtime source unless a refactor clearly changes the entrypoint.
- Keep notification behavior consistent across `notifications/` code and `docs/notification_rules.json`.
- Check migration impact before changing order, bonus, or payroll-related data flows.
- Record environment-sensitive changes clearly because webhook and token settings matter to runtime behavior.
- This is an old repository: verify whether a directory is still live before editing it.

## Project Rules (2026-09-10)

Common rules for python projects (Rules 4.1):

- At 300k tokens of memory the coordinator or the executor runs `/compact` stating what to keep, or closes the session by the closing rules.
- Tests during work are targeted: only the affected file or a selection (`pytest tests/x.py -q --tb=short`), output through `tail`. A full run once before commit and once before deploy. TDD stays: this project has a standard `pytest`.
- `ssh` only through the project's runbook scripts, output trimmed to the useful part. No manual step-by-step diagnosis on the server from the main session: write a script and run it once.
- Files longer than 500 lines are read in parts; the function map lives next to the file (owner decision 2026-09-10).
- `.claude/settings.json` of this project holds the permissions its own work needs: `pytest`, `git status/diff/log`, file reads (`cat`, `sed -n`, `grep`, `rg`), `python -m`, project scripts. `ssh`, `scp`, `rsync` stay a question for the owner.
- Subagent worktrees are removed after merge (`git worktree prune` plus branch deletion). No leftovers between sessions.
- Images and screenshots do not go into working-session memory, unless the task is about the interface and the owner chose to show the screen.

This project (Rules 4.2):

- Splitting `bot.py` into modules is discussed separately as a project decision; do not start it on your own.

Files longer than 500 lines and their function maps:

- `bot.py` (15004 lines) -> `bot.py.map.md`
- `raketa-admin-bot/adminbot/db.py` (1200 lines) -> `raketa-admin-bot/adminbot/db.py.map.md`
- `cleaning/handlers.py` (1487 lines) -> `cleaning/handlers.py.map.md`

## Deploy Rules

- Deploy only from committed and pushed state.
- If the task affects prod runtime, verify the relevant runbook in `project.md`, `scripts/`, or other project docs before deployment.
- If no trusted deploy sequence is documented for the task, stop and document that gap instead of guessing.
- Assume the path is `local -> git -> VPS` unless project docs say otherwise.
- Do not search for passwords, invent credentials, or guess how to get onto the server.
- If SSH works but `sudo` or another privileged step is unavailable, stop and ask the user.

Two services, two runbooks — they are not interchangeable:

- Worker bot: `docs/deploy.md`. Branch `main`, `git pull --ff-only origin main` in
  `/opt/telegram-bot`, restart `telegram-bot.service`.
- Admin bot: `raketa-admin-bot/docs/deploy.md` §9. Two steps: `rsync` from
  `~/Projects/raketaclean/raketa-admin-bot/` to `/home/admin/raketa-admin-bot/`, then
  `sudo raketa-admin-bot-update`. A bare `update` without the `rsync` brings up the old version.

## End Of Session Requirements

Before ending the session:
1. run `git status --short`;
2. commit completed work in one or more small logical commits;
3. rewrite `./AGENT_STATE.md` to reflect current state;
4. add one new entry at the top of `./SESSION_LOG.md`;
5. apply the context-file rules from the global `~/.claude/CLAUDE.md` to both files.

## Current Focus

Prepare safe future work on Google Calendar integration while preserving the currently stable production flows.
