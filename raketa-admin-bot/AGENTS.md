# AGENTS.md — admin bot (`raketa-admin-bot/`)

<!--
  Адаптировано из CLAUDE.md для Codex, 2026-09-21.
  Правила корня репозитория находятся в ../AGENTS.md и автоматически
  наследуются. Этот файл задаёт только правила admin bot.
-->

## Goal

Owner-only admin bot for the cleaning company (raketaclean): a separate service on the VPS that automates administrative work the owner does by hand. Feature v1: automatic amoCRM deal processing driven by the worker bot's database (design approved 2026-08-24). Future features: calendar sync, partner Excel processing, amo→bot import replacement.

## Read Order

Infra map (canonical, token-light): /Users/evgenijpastusenko/Projects/agent1/docs/INFRA_MAP_LITE.yaml

1. `../AGENT_STATE.md` in the repository root (context of the whole monorepo)
2. recent entries in `../SESSION_LOG.md` there
3. `docs/plans/2026-08-24-amo-sync-design.md` (approved design)
4. current implementation plan in `docs/plans/`
5. recon facts: `docs/recon/` in the repository root (00-summary, 06-matching-metrics)

## Hard Rules (project-specific)

- NEVER write to the worker bot's tables (schema `public` of the shared DB): read-only. All own state lives in a dedicated schema.
- All amoCRM writes go through the dedicated write-scoped integration, never through the worker bot's read-only token.
- Every feature has its own kill switch; default off until owner enables.
- PII (client phones, names, addresses) never goes into git; mask phones to last 4 digits **in logs** (`adminbot.phone.mask`).
- Messages to the owner show the **full phone and the order date** (`adminbot.phone.for_owner`): the bot is private, he is its only recipient, and he needs to reach the client without opening CRM. Owner's decision 2026-08-26 — do not "restore" masking there.

## Deploy

Two steps, both of them, in this order. Full runbook: `docs/deploy.md` §9.

1. `rsync` the code from `~/Projects/raketaclean/raketa-admin-bot/` to
   `admin@91.200.150.68:/home/admin/raketa-admin-bot/`.
2. `sudo raketa-admin-bot-update` — installs into `/opt/raketa-admin-bot/app` and restarts
   `raketa-admin-bot.service`.

A bare `update` without the `rsync` brings up the old version. The move into the monorepo
changed only the local source path; the server paths are the same as before.

## Function Maps

Files longer than 500 lines and their function maps:

- `adminbot/db.py` (1438 lines) -> `adminbot/db.py.map.md`

## Current Focus

Turn the approved design into an implementation plan (`docs/plans/`), then build v1 (amo_sync) step by step: skeleton → matcher + history exam → amo write client → Telegram UI → rehearsal → backlog run from 2026-08-21.

