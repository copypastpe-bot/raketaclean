### # SESSION_LOG_archive

Общий архив монорепозитория `raketaclean` (с 2026-09-14). Собран из журналов и архивов двух прежних проектов; внутри каждого раздела записи идут в том порядке, в каком лежали. Только дополняется.

## Из журнала tgbot-v1

_Прежняя шапка: «Archived entries from `SESSION_LOG.md` (newest archived at the bottom; append, never delete).»_

### 2026-09-10 - Отмена: задачи и причина; ТЗ письма клиенту и уборок; разведка «не купили»

status: completed; `6a7df88` выкачено 18:00 МСК, хеши сверены
actor: claude (Fable: разведка, ТЗ, код по слову владельца «делай сам»)
scope: `feature/amo-sync-v1` → `fec1960`, pushed. 783 теста зелёных, 4 новых.

- **Отмена по «🔒 Закрыть сделку»** теперь закрывает все открытые задачи
  («🤖 Заказ отменён: запись удалена из календаря»), ставит «Причина
  закрытия = Пропала потребность» (19215/8299, сверено на боевой амо; чужое
  значение не трогает) и лишь потом закрывает сделку. Порядок «спросить →
  кнопка» не менялся. ТЗ: `docs/plans/2026-09-10-cancel-tasks-and-reason.md`.
- **ТЗ для tgbot-v1** `2026-09-10-cancel-letter-to-client.md`: письмо
  «заказ отменён» по любой сделке с этой причиной, только тем, кому писали;
  текст владельца; не слать, если день работы прошёл; выключатель общий с
  «заказ принят». Запускает владелец новой сессией.
- **Уборки в амо не проводятся**: клининг пишет в `cleaning_orders`, робот
  читает `orders`. ТЗ `2026-09-10-cleaning-orders-sync.md` с решениями
  владельца (Ольга, Уборка, оплаты, хвост с включения). Запуск не решён.
- **Лиды «не купили»** отложены: запись звонка уже лежит ссылкой в примечании
  сделки и открыта без пароля; причины отказа амо выключены; у владельца только
  DeepSeek. Замер: на «Новый лид» 20 сделок.
- Ошибка деплоя: `update` без rsync перезапустил старую версию — поймано по
  хешу, повторено по runbook §9. Правило про два шага записано в состояние.

deploy: `6a7df88`, служба active
---

### 2026-09-06 — адрес как свойство заказа

status: выкачено в прод 16:06 MSK по решению владельца; живой заказ ещё не прошёл
actor: claude (Fable — контроль и ТЗ; реализация Opus; проверки Sonnet и Opus)
scope: tgbot-v1 `feature/amo-exchange` → `cee1497` (pushed), 288 тестов (+48).

- ТЗ из `raketa-admin-bot/docs/plans/2026-09-06-order-address-handoff.md`: отчёт
  о проведении печатал адрес карточки, актуальный живёт в календаре → сделке амо.
- Сделано: колонка `orders.address`; при проведении бот ищет контакт по телефону,
  его сделки, открытую сделку реализации не дальше двух суток; адрес идёт в заказ,
  отчёт и подпись кассы; `last_order_addr` перезаписывается. CRM недоступна или
  сделки нет — карточка, как раньше; поход до транзакции, 8 с, «⏳ Провожу заказ…».
- Решения владельца: `last_order_addr` перезаписывать, `address` не трогать;
  подпись кассы из адреса заказа. Окно в двое суток — решение агента.
- Sonnet упал по лимиту (429) на тестах — тесты написала Fable. В тот же час
  владелец ввёл правило маршрутизации моделей (CLAUDE.md `c2035ec`) для новых сессий.
- Проверка качества нашла два важных: окно повторного тапа мастера и выбор сделки
  без предела дальности. Оба закрыты (`f834829`, `cee1497`).
- Хвосты: живая проверка первого заказа (план §9); `raketa-admin-bot` `adminbot/db.py:43`
  читать `o.address`; превью «Проверьте» показывает карточку, отчёт — сделку;
  `fetch_contact_leads` в проверке новых лидов опирается на фильтр, который амо
  игнорирует (отдельная задача).

deploy SHA: `cee1497` (старт чистый, колонка `orders.address` на месте).
---

### 2026-09-06 - Вторая сделка по одному заказу: причина, лечение, адрес

status: completed; выкачено, служба перезапущена 13:18 МСК
actor: claude
scope: `feature/amo-sync-v1` → `13e0ede`, pushed. 779 тестов зелёных, 3 новых.

- **Причина второй сделки найдена**: список «занятых» сделок строится по ВСЕМ
  записям телефона без учёта статуса, и удалённая запись держала сделку за
  собой. Кандидатов не остаётся → тихий путь «клиента нет, завожу с нуля»;
  воспроизведено на живом коде. Владелец: запись удалил по невнимательности,
  но поведение робота системное — повторилось бы при любом удалении.
- **Ключ `--gcal-forget=ID[,ID]`** (+ `--gcal-forget-live`): забыть запись,
  которой уже нет в календаре — `--gcal-run` её не находит. Показывает
  освобождаемые сделки и долги очереди, гасит их как «нужда отпала».
- **Случай вылечен**: мёртвая запись забыта, лишнюю цепочку в амо владелец
  удалил руками (робот удалять не умеет), живая запись проведена заново с
  `--gcal-reset` и привязана к 31587353 со свежими данными календаря.
- **Адрес не затирается**: проведение заказа переписывало адрес сделки адресом
  из карточки клиента. Теперь «заполненное не трогаем», как в ковровом движке.
- ТЗ для `tgbot-v1`: `docs/plans/2026-09-06-order-address-handoff.md`.
- Запись «fd6767b не развёрнут» была неверной: код в бою с 04.09.

deploy: `13e0ede`, служба active
### 2026-09-03 - all three bots moved onto a proxy outside RU, watchdog added

status: live on prod, watchdog on cron
actor: claude
scope: tgbot-v1, branch `feature/amo-exchange` at `1c0958d` (pushed, prod). 188 tests green, 12 new.

- tinyproxy on Contabo (NL), container `telegram-proxy`, port 39443, built as a Docker image
  because sudo there needs a password we do not have (`deploy` is in the `docker` group).
- Four barriers, each verified separately: Allow only 91.200.150.68 (proved by swapping the
  rule to a foreign IP and watching prod get refused), 40-char Basic auth, CONNECT to 443
  only, one-host whitelist. The port is not reachable from an arbitrary address.
- **Result: 30 of 30 requests through the proxy, none over a second**, against 4 of 30
  hanging 10 s directly a day earlier. Main bot: 0 failures and 0 restarts in the first hour.
- **Cost: 3.5 min of downtime** (11:42:28-11:45:50). aiogram needs `aiohttp-socks` for any
  proxy, not just SOCKS; the bot crash-looped until it was installed. Now in `requirements.txt`.
- The spec's worry about the token leaking into proxy logs was wrong: with CONNECT the proxy
  only sees `api.telegram.org:443`. Corrected in the document.
- Watchdog `scripts/telegram_proxy_watchdog.py`, cron every 5 min. Its alert goes **around the
  proxy**, direct through the IP pool - otherwise the alarm would depend on what it reports.
  Verified live, delivered to both admins. Decision logic is a pure function, 12 tests.
- **All three bots moved the same day**: main 11:45, client 12:59 (the owner ran that restart -
  the unit is not in NOPASSWD), admin 13:22 after he decided to do that repository's change in
  this session. 0 direct connections to Telegram left on the host. The admin bot's code, its
  `--telegram-proxy` key and its context are committed in its own repo (`2b6c0ee`), including
  a bug I introduced there and fixed: an undeclared variable under `set -u` broke its plain
  update command.

deploy SHA: `1c0958d`

---

### 2026-09-02 - bot hangs traced to Telegram blocking plus our own resolver

status: patch deployed to prod, proxy spec written for 2026-09-03
actor: claude
scope: tgbot-v1, branch `feature/amo-exchange` at `a0f5704` (pushed), prod `e116497`. 176 tests green, 10 new.

- The owner reported the bot freezing after a button press, worst after an idle gap, and
  the client bot often not answering. He guessed blocking; right, but half the delay was ours.
- Measured on prod: 4 of 5 addresses in `TELEGRAM_API_IPS` dead, the live one loses ~1 probe
  in 6, 4 of 30 real requests hung 10 s, and blocking comes in waves that kill every address
  at once (one caught live, 49 in 7 days of logs). Contabo NL reaches Telegram 5/5 at 0.3 s.
- Three faults in our resolver: the working address was probed **last** (four dead ones first
  at 1.5 s each, every 30 s - exactly the 6-second wait he described); the probe was a bare
  TCP connect, so a TLS-blocked address passed as alive and held the bot silent 30 s (13
  times in 7 days); one lost packet was enough to abandon the good address.
- Fixed: TLS handshake with SNI as the probe, current address rechecked first with a second
  attempt, the rest probed in parallel, timeouts from env. Resolver moved to
  `telegram_transport.py` so it could be tested at all - `bot.py` is too heavy to import.
  10 tests, verified to fail against the old logic before committing.
- Waves are not fixed by this: spec at `docs/plans/2026-09-03-telegram-proxy.md`. The client
  bot has no workaround at all and needs its own session. Cleaning-till withdrawal command
  deferred by the owner to 2026-09-03.

deploy SHA: `e116497`

---### 2026-08-29 - birthday letters were quoting a balance that no longer existed

status: fixed and deployed
actor: claude
scope: tgbot-v1 at `201ca21` (pushed, prod). 166 tests green.

- A client called: «260 bonuses disappeared». They had not: her 2025 birthday gift expired
  on schedule after a year. But the birthday letter that same morning told her **560**,
  while her real balance was already 300.
- Cause found in `run_birthday_jobs`: accrual ran first and took the balance for the letter,
  expiry ran on the next line and changed it 80 ms later. The letter carried a number that
  existed for a fraction of a second. Fixed by swapping the two calls — expiry first.
- **329 clients** got birthday letters with an inflated balance, the latest on 2026-08-28.
  The owner decided not to chase them; he credited that one client 260 back by hand.
- His manual credit went into `clients.bonus_balance` only, so her card shows 860 while the
  transaction history sums to 600. Those 260 will never expire (expiry walks the history).
  Offered to write a matching transaction; owner has not answered.
- The regression test reads `bot.py` with ast and checks the call order. Unusual, but the
  bug lives exactly in the order of two lines and cannot be reproduced without a database.
  Verified it fails on the old order before committing.

deploy SHA: `201ca21`

---### 2026-08-28/29 - client messaging built AND put live; exchange switched to live too

status: deployed, both features live, «Да» proven end to end
actor: claude
scope: tgbot-v1, branch `feature/amo-exchange` at `078b38c` (pushed, prod). 164 tests green, 54 new.

- All seven plan tasks built, then the trigger was **rebuilt on live facts**: a deal
  never moves into «Заказ оформлен», it is created there (0 such events in a week
  vs 40 «Передано в работу»). The real trigger is the primary pipeline's win — the
  same event the exchange already watches.
- The owner explained the process and the data confirmed it: his robot reads the
  calendar, fills date and address in the lead, moves it to «Передано в работу»,
  and creates a child deal in the realization pipeline the same minute. amoCRM
  writes a `lead_auto_created` note — that link says which deal «Да» must move.
- amoCRM **write access confirmed** on the prod token (wrote back the same status,
  nothing changed). First write capability in this bot's history.
- Exchange switched to live: first pass created 1 client, updated 2 cards, zero
  duplicate phones. Client messaging ran a rehearsal on the owner's test order,
  then went live; the first real letter reached him via WhatsApp.
- **Bug found by the owner on that letter**: the address read «адрес». The lead's
  address is auto-filled from the contact, while the real one lives in the child
  deal. Both the letters and the exchange now read the deal, lead is the fallback.
- `last_order_addr` now refreshes with every order (owner's decision): the column
  means «address of the last order», and «fill only when empty» made it «address
  of the first order» — the junk sat in the card for years.
- **Loop closed on live data**: the owner answered «Да», the child deal moved to
  «Заказ подтвержден» within a second, and he was not disturbed — which read to him
  as «not caught», since a silent success leaves no trace outside CRM. On «Да» the
  client now gets «Спасибо. Специалист позвонит за 30 минут до приезда.»
- Event log pinned the address bug to the second: 20:38:20 the robot wrote the real
  street, 20:38:21 the stage changed, 20:38:21 a CRM trigger overwrote it from the
  contact. It only fires for repeat clients — whose contact has an address — which
  is why three site leads that day looked clean. Owner found and deleted the trigger.
- New-lead alerts filtered: a lead with a work date or a child deal is a placed
  order, not a request. That was 30 of 57 alerts a week. The check runs before the
  status check on purpose — the robot moves the lead within a second, so by the time
  the poll arrives it is no longer «Новый лид».
- Still unproven: everything that calls the owner — «Нет», unclear answers, silence.

deploy SHA: `078b38c` (branch `feature/amo-exchange`, both features live)

---### 2026-08-28 - client messaging before the job built end to end (tasks 1-7)

status: superseded the same evening by the entry above
actor: claude
scope: tgbot-v1, branch `feature/amo-exchange` at `2fb65b2` (pushed). 149 tests green, 39 new.

- Built all seven tasks of `docs/plans/2026-08-28-client-messaging-implementation.md`:
  `order_confirmations` table, pure rules in `notifications/client_messaging.py`,
  both letter texts, poller of «Заказ оформлен», answer parsing in
  `handle_wahelp_inbound`, silence watch, switch + rehearsal + daily summary.
- **First write to amoCRM in this bot** (`patch` / `update_lead_status`): the client
  could only read until now. Whether the prod token may write is unverified; a denied
  write raises and the owner is told — silence would leave him sure the CRM matched.
- Two deviations from the plan, both deliberate: «не» dropped from the refusal list
  («не знаю» is a conversation, not a refusal, and would have inflated the refusal
  count in his report); and no `asked` status — rows stay `planned` with `asked_at`
  holding the planned question time, because the owner ruled the reason for silence
  does not matter.
- Branch order matters: явные «да»/«нет» go to confirmation first, anything unclear
  is offered to the older branches (rating, STOP, promo) before the owner — otherwise
  a «5» from a client awaiting confirmation would be lost as an unclear answer.
- Rehearsal leaves no traces (cursor in memory, own journal key, no rows, no letters),
  so the live run starts clean. Cost: answer parsing and the CRM write get their first
  real proof only in live mode.
- Also closed a plan warning by fact: the rules path is hardcoded, and
  `docs/notification_rules.json.local` is read by nobody.
- Not deployed by design. Next: `scripts/check_order_created_events.py` on the server,
  then deploy, then `CLIENT_MESSAGING_ENABLED=1` with `DRY_RUN=1`.

deploy SHA: unchanged (`ff95fc8` on prod)

---### 2026-08-28 - amoCRM auto-exchange built; client messaging designed and deferred

status: code complete, not deployed
actor: claude
scope: tgbot-v1, branch `feature/amo-exchange` at `b96c291` (pushed). 108 tests green, 25 new.

- **Why**: the owner exported deals to CSV weekly by hand, and the import silently
  lost data — it looks up columns by name, and the export template in CRM had
  changed, so name, bonuses and birthday never arrived while the import reported
  success. Checked on his real file: 62 deals, 58 phones, and 3 refusals would have
  become clients under the old address-based rule.
- Built `notifications/amo_exchange.py` (pure rules + amo parsing, 25 tests) and a
  thin layer in `bot.py`: poller with its own cursor (`exchange_events`), event
  dedup, reading and writing `clients`, switch plus rehearsal mode, daily summary
  counted from the event journal so it survives restarts.
- Owner's rules: fills address, service and empty name only. Never bonuses,
  birthday, district or last order date — they are born in the bot. **A client is
  never demoted to lead**; a lost deal of a known person changes nothing.
- A live check changed the design: `lead_status_changed` events carry the new status
  and pipeline, so foreign deals (hundreds a day) are filtered without fetching each
  card. The decision comes from the event, not the card — a deal moved further still
  means the order was placed.
- **Deployed to prod in rehearsal** the same evening. Two bugs caught there, not by
  tests: the rehearsal shared its cursor and event journal with the live run (a day
  of rehearsal would have made the live run start from «nothing changed» — the same
  mistake that ate the partner's letters on 2026-08-26), and the last event was
  recounted every pass, filling the log.
- Owner's idea, implemented the same evening: **two passes at different speeds** —
  won every 10 minutes on its own schedule, lost in one batch on Mondays at 10:00.
  Nobody messages a refusal, so a weekly batch is easier to check than a trickle.
  The poll stays as insurance for deals booked past the robot.
- Rehearsal on real data: 4 deals of the day, 3 clients unknown to the bot's
  database — exactly the gap the exchange closes.
- Client messaging: design plus a 7-task plan written
  (`docs/plans/2026-08-28-client-messaging-*.md`), texts approved by the owner.
  Not started; blocked until the exchange runs live.
- Deploy SHA: `ff95fc8` (branch `feature/amo-exchange`, rehearsal mode).

---### 2026-08-27 12:40 - Cleaning contour put into production for Olga

status: done (prod data + env change, no code changes)
actor: claude
scope: Onboarded the real cleaner, wiped test cleaning data, entered the opening cash balance, fixed the cleaning money-flow chat id.

- Cleaner `tg=5195851358` renamed from «Клинер» to **Ольга Скоропашкина, +79081572721** in both `staff` and `cleaning_foremen`; role `cleaner` and command menu already in place, no restart needed for that.
- Test foreman `tg=7671577717` («Тест Бригадир») intentionally kept active — owner uses it himself.
- Test cleaning order #1 (10 000 ₽, 21.05) cancelled by SQL equivalent of `/cleaning_cancel_order`: order + 3 cashbook rows soft-deleted, mirror bonus rows written, test client 5108 back to 800 bonuses. Cleaning cash went 4 700 → 0.
- Opening balance entered by owner via `/cleaning_cash_add`: `deposit` «Наличные» 3 793 ₽. Deposit is excluded from P&L by design, so it lifts the balance without touching profit.
- **Env change:** `CLEANING_MONEY_FLOW_CHAT_ID` `-3687084157` → `-1003687084157` (group had become a supergroup, old id gave `Bad Request: chat not found`). Backup at `/opt/telegram-bot/.env.bak-20260827`; service restarted; delivery confirmed by owner with a 1 ₽ test deposit, which was then deleted.
- Final state: cleaning cash 3 793 ₽, one cashbook row, zero active cleaning orders, money-flow alerts working.
- One-page Russian instruction sent to Olga's private bot chat (owner-approved text, message 39637).
- Diagnostic finding: on the VPS DNS returns an unreachable address for `api.telegram.org` (IPv6 plus IPv4 `149.154.166.110`); the working path is `curl --resolve api.telegram.org:443:149.154.167.220`, which is how the bot itself is connected.
- Open items for owner: whether Olga needs `cleaning_view_reports`; whether the 5 500 ₽ minimum fits real cleaning jobs; optional one-page instruction for Olga.

deploy SHA: 8151325 (unchanged)

---### 2026-08-24 21:40 - Designed amo-sync v1 and new raketa-admin-bot service

status: design approved
actor: claude
scope: Brainstorming (superpowers skill) with owner on top of finished recon; design doc written and committed in NEW repo raketa-admin-bot. No tgbot-v1 changes.

- Owner decisions: separate admin-bot service (future platform for owner-only admin functions), NOT a module of prod bot; v1 = full amo deal cycle driven by bot DB (find/create lead -> Передано в работу -> salesbot autodeal -> ЗАКАЗ ВЫПОЛНЕН и Оплата получена); calendar = phase 2, carpets Excel = phase 3, CSV import replacement = phase 4.
- Behavior: auto when unambiguous, TG card with buttons when not; instant processing + 21:00 MSK reconciliation summary; close amo autotasks; close "Получить ОС" if client already rated in bot; Услуга default by master (Никита/Дима=мебель, Оля=клининг); budget = full check.
- Safety: read-only on bot DB (own schema for state), new amo integration with write scope, per-feature kill switch, idempotent step checklist, backlog from 2026-08-21 runs with dry-run preview + "Поехали" button.
- New repo: ~/Projects/raketa-admin-bot, design at docs/plans/2026-08-24-amo-sync-design.md (commit 2ac4614).
- Next session: invoke superpowers:writing-plans for implementation plan (session task #6).

deploy SHA: 8151325 (unchanged)

---### 2026-08-24 18:30 - amoCRM automation recon (coworker brief) completed

status: done (no code/prod changes)
actor: claude
scope: Read-only recon of amoCRM, bot code/DB, Google Calendar, orders chat, partner Excel; all matching metrics computed; reports written to tgbot-v1/recon/ (uncommitted by owner decision).

- amo structure dumped via bot token (read-only): 7 pipelines, 65 lead fields (53 dead), 3 users, 1 webhook; autotasks by robot confirmed from 897 tasks/90d.
- Metrics: calendar->amo 87% auto (M1 70 + M2 17), bot->amo 92%, partner Excel->amo 90-96%, phone extract from calendar 98.5%, contact dups 1.4%, M9 weekday-only dates 0% in texts (but present in note screenshots).
- Facts vs brief: salesbot creates 2nd deal per order; cancellation = calendar event deletion (no "отменен" comments); chat photos = calendar screenshots; carpet orders dictated to partner by phone, chat is internal-only; "Ковры Кристал" = partner Kristall; amo token read-only.
- Bot risk logged: 5 phone-normalization families; canonical char-scan (bot.py:3251) must become the standard for matching.
- Calendar raketaclean52@gmail.com shared read-only to copypast.pe@gmail.com; 462 events pulled (6 mo).
- Owner decisions: roptick excluded; recon/ stays local uncommitted; recon/data/ (PII) gitignored.
- Next: architecture phase on top of recon/00-summary.md; will need amo integration with write scope.

deploy SHA: 8151325 (unchanged)

---### 2026-08-13 10:55 - Post-deploy verification of 2026-08-12 changes

status: verified
actor: claude
scope: Checked logs, deliveries, nightly jobs, n8n output and backups after the 2026-08-12 deploy. No code changes.

- `telegram-bot.service` up on `8151325` for 20h, 0 restarts, 0 error-level entries, 0 tracebacks.
- 3 `order_completed_summary` messages delivered with the new short text (2 WhatsApp, 1 Telegram); 3 `order_rating_reminder` queued for +24h.
- Contact footer confirmed on prod by calling `_with_contact_footer` in the prod venv: `send_with_rules` applies it, links correct.
- `rewash_counter` ran 10:00 MSK — the kept part of the rewash code survived the 143-line deletion.
- n8n overnight batch (13.08 09:00 UTC): promo 10/10 and birthday 5/5 carry the new MAX link and TG chat, zero old bot links, zero `⚠️`.
- Backup 03:40 created and replicated offsite; both copies present.
- Note for future checks: `notification_messages.message_text` stores the text BEFORE the footer is glued, and journald truncates the send log after the second line — neither proves what the client received.
- `promo_reminders` / `birthday_bonuses` had not run yet at check time (scheduled ~11:00 / ~12:00 MSK); first run of the promo code without stage 2 not yet observed.

deploy SHA: 8151325 (unchanged)

---

### 2026-08-29 - birthday letters were quoting a balance that no longer existed

status: fixed and deployed
actor: claude
scope: tgbot-v1 at `201ca21` (pushed, prod). 166 tests green.

- A client called: «260 bonuses disappeared». They had not: her 2025 birthday gift expired
  on schedule after a year. But the birthday letter that same morning told her **560**,
  while her real balance was already 300.
- Cause found in `run_birthday_jobs`: accrual ran first and took the balance for the letter,
  expiry ran on the next line and changed it 80 ms later. The letter carried a number that
  existed for a fraction of a second. Fixed by swapping the two calls — expiry first.
- **329 clients** got birthday letters with an inflated balance, the latest on 2026-08-28.
  The owner decided not to chase them; he credited that one client 260 back by hand.
- His manual credit went into `clients.bonus_balance` only, so her card shows 860 while the
  transaction history sums to 600. Those 260 will never expire (expiry walks the history).
  Offered to write a matching transaction; owner has not answered.
- The regression test reads `bot.py` with ast and checks the call order. Unusual, but the
  bug lives exactly in the order of two lines and cannot be reproduced without a database.
  Verified it fails on the old order before committing.

deploy SHA: `201ca21`

---

### 2026-08-28/29 - client messaging built AND put live; exchange switched to live too

status: deployed, both features live, «Да» proven end to end
actor: claude
scope: tgbot-v1, branch `feature/amo-exchange` at `078b38c` (pushed, prod). 164 tests green, 54 new.

- All seven plan tasks built, then the trigger was **rebuilt on live facts**: a deal
  never moves into «Заказ оформлен», it is created there (0 such events in a week
  vs 40 «Передано в работу»). The real trigger is the primary pipeline's win — the
  same event the exchange already watches.
- The owner explained the process and the data confirmed it: his robot reads the
  calendar, fills date and address in the lead, moves it to «Передано в работу»,
  and creates a child deal in the realization pipeline the same minute. amoCRM
  writes a `lead_auto_created` note — that link says which deal «Да» must move.
- amoCRM **write access confirmed** on the prod token (wrote back the same status,
  nothing changed). First write capability in this bot's history.
- Exchange switched to live: first pass created 1 client, updated 2 cards, zero
  duplicate phones. Client messaging ran a rehearsal on the owner's test order,
  then went live; the first real letter reached him via WhatsApp.
- **Bug found by the owner on that letter**: the address read «адрес». The lead's
  address is auto-filled from the contact, while the real one lives in the child
  deal. Both the letters and the exchange now read the deal, lead is the fallback.
- `last_order_addr` now refreshes with every order (owner's decision): the column
  means «address of the last order», and «fill only when empty» made it «address
  of the first order» — the junk sat in the card for years.
- **Loop closed on live data**: the owner answered «Да», the child deal moved to
  «Заказ подтвержден» within a second, and he was not disturbed — which read to him
  as «not caught», since a silent success leaves no trace outside CRM. On «Да» the
  client now gets «Спасибо. Специалист позвонит за 30 минут до приезда.»
- Event log pinned the address bug to the second: 20:38:20 the robot wrote the real
  street, 20:38:21 the stage changed, 20:38:21 a CRM trigger overwrote it from the
  contact. It only fires for repeat clients — whose contact has an address — which
  is why three site leads that day looked clean. Owner found and deleted the trigger.
- New-lead alerts filtered: a lead with a work date or a child deal is a placed
  order, not a request. That was 30 of 57 alerts a week. The check runs before the
  status check on purpose — the robot moves the lead within a second, so by the time
  the poll arrives it is no longer «Новый лид».
- Still unproven: everything that calls the owner — «Нет», unclear answers, silence.

deploy SHA: `078b38c` (branch `feature/amo-exchange`, both features live)

### 2026-05-20 13:05 - Added amoCRM webhook alert implementation

status: completed
actor: codex
scope: Added the first amoCRM inbound webhook path for urgent admin alerts about new requests.

- Added design/implementation plan under `docs/superpowers/`.
- Added `/amocrm/webhook`, token handling, parser/formatter, DB bootstrap, and admin Telegram alert handler.
- Added env keys `AMOCRM_WEBHOOK_TOKEN` and `AMOCRM_ACCOUNT_DOMAIN`.
- Commits: `8ea1dde`, `05576da`, `1d200d1`; not pushed/deployed in that session.

---

### 2026-05-11 13:35 - Added MAX promo routing switch

status: completed
actor: codex
scope: Added a narrow routing control so MAX can stay enabled for service notifications while being disabled for promo traffic.

#### Changes

- Added `WAHELP_CLIENTS_MAX_PROMO_ENABLED` handling in `crm/wahelp_dispatcher.py`.
- Treated `promo_reengage_*` and `birthday_congrats_*` as promo events for MAX routing.
- Filtered MAX from both normal send cascade and retry candidate selection for promo events only.
- Added the new env knobs to `.env.example`.

#### Notes

- Local repo was committed as `ff77355`.
- Production was inspected, but this switch was not deployed in this session.

#### References

- `crm/wahelp_dispatcher.py`
- `bot.py`
- `.env.example`

---

### 2026-04-13 17:05 - Added client-bot heartbeat alerts to admin bot

status: completed
actor: codex
scope: Added a narrow operational alert path so the admin bot reports when the companion client Telegram bot stops updating its heartbeat or loses Telegram API health.

#### Changes

- Added shared `service_heartbeats` schema bootstrap in `bot.py`.
- Added a periodic `check_client_bot_health()` job with one-shot alert and recovery notifications to admins/log chat.
- Added env knobs for the monitored service key, display name, check interval, and stale threshold in `.env.example`.
- Coordinated the design with the companion `telegram-bot-client` repo so both runtimes use the same heartbeat contract.

#### Verified

- Read the live scheduling/bootstrap path in `bot.py` and attached the health check without touching order, payroll, or notification business flows.
- Reviewed the resulting diff to confirm the patch stays local to runtime bootstrap and alerting.
- Ran syntax validation via `compile(...)` on `bot.py`.

#### Next Steps

- Deploy both repos together so the client bot starts writing heartbeats before or alongside the admin-bot checker.
- After deploy, confirm that `service_heartbeats` contains `telegram-bot-client` updates and that admins receive a recovery message if the checker had already opened an alert.

#### References

- `bot.py`
- `.env.example`
- `AGENT_STATE.md`
- `../telegram-bot-client/bot.py`

---

### 2026-04-15 14:20 - Restored broken prod backups and tracked operational scripts

status: completed
actor: codex
scope: Investigated why Telegram bot backups stopped after 2026-04-10, restored the missing production backup script, and fixed the repository so future deploys keep the operational scripts.

#### Changes

- Diagnosed prod cron backup failures on `admin@91.200.150.68` and confirmed `/opt/telegram-bot/scripts/backup_telegram_bot.sh` was missing from the server.
- Restored the backup script on prod, ran a manual backup successfully, and confirmed fresh local plus offsite copies were created.
- Added `scripts/backup_telegram_bot.sh` and `scripts/reset_and_restart_bot.sh` to tracked repo state so deploys do not silently remove them again.
- Refreshed `AGENT_STATE.md` with the operational-script tracking requirement.

#### Verified

- Confirmed cron still calls `/opt/telegram-bot/scripts/backup_telegram_bot.sh` at `03:40`.
- Verified the recovered run created `tgbot_db_2026-04-15_14-07-22.sql.gz` and `tgbot_config_2026-04-15_14-07-22.tar.gz` under `/opt/telegram-bot/backups/`.
- Verified matching offsite copies appeared under `/home/deploy/backups/telegram-bot/` on `crm-offsite-backup`.
- Re-checked local git state before staging the scripts.

#### Next Steps

- Commit and deploy the newly tracked operational scripts from clean git state.
- After the next normal cron window, confirm the scheduled `03:40` backup succeeds without manual intervention.

#### References

- `scripts/backup_telegram_bot.sh`
- `scripts/reset_and_restart_bot.sh`
- `AGENT_STATE.md`
- `project.md`

---

### 2026-05-02 12:34 - Queried cashbook entries for 2026-04-08

status: completed
actor: codex
scope: Read-only production DB check for cashbook movement on April 8.

#### Changes

- No repository code changes.

#### Verified

- Connected to `clients_db` as `bot`.
- Confirmed `cashbook_entries` had 4 non-deleted rows and no deleted rows for 2026-04-08 MSK before restoration.
- Calculated day income, expense, and running cash balance using the bot's `get_cash_balance_excluding_withdrawals` rule.

#### References

- `cashbook_entries`
- `orders`
- `order_payments`

---

### 2026-05-02 12:45 - Fixed split-payment cashbook recording and restored affected rows

status: completed
actor: codex
scope: Fixed a production cashbook bug where split-payment order income overwrote one cash row instead of creating one row per payment part.

#### Changes

- Updated `bot.py` so `_record_order_income()` always inserts a new `cashbook_entries` row.
- Committed and pushed the code fix as `e45e7a7 Fix split payment cashbook entries`.
- Restored production cashbook detail for split-payment orders `124`, `162`, `223`, `245`, and `345`.
- Updated `jenya_card_entries` mirror for cash entry `283` from `7300.00` to `2300.00`.

#### Verified

- Ran `compile(...)` syntax check for `bot.py`.
- Verified all split-payment orders now have `cash_sum - payments_sum = 0`.
- Verified 2026-04-08 MSK cashbook now has 5 rows, income `24100.00`, expense `13775.00`, day delta `+10325.00`, start balance `107998.84`, end balance `118323.84`.
- Verified current bot cash balance is `179854.84`.

#### Notes

- Historical orders `1`-`33` have `order_payments` without matching `cashbook_entries`; this is separate from the split-payment overwrite bug and was not restored in this session.
- User later pulled the commit on the server and restarted the production service.

#### References

- `bot.py`
- `cashbook_entries`
- `order_payments`
- `jenya_card_entries`

---

### 2026-05-02 13:14 - Sent cashbook correction notices to finance chats

status: completed
actor: codex
scope: Posted user-approved correction summaries to Telegram finance chats after split-payment restoration.

#### Changes

- Sent a correction summary to `Ракета Деньги` (`MONEY_FLOW_CHAT_ID`, message_id `9405`).
- Sent a separate `Женя деньги` correction for order `162` (`JENYA_CARD_CHAT_ID`, message_id `558`).

#### Verified

- Telegram Bot API returned `ok: true` for both messages.
- Included corrected current balances: cashbook `179854.84`, Jenya card `6282.84`.

#### References

- `cashbook_entries`
- `jenya_card_entries`
- Telegram chats: `Ракета Деньги`, `Женя деньги`

---

### 2026-05-06 18:08 - Added April 30 cashbook transactions

status: completed
actor: codex
scope: Inserted user-provided production cashbook corrections for April 30 through the local PostgreSQL tunnel.

#### Changes

- Added two `cashbook_entries` income rows for `р/с`: `ковры Михалкова` and `теплоход Отчизна`.
- Added five `cashbook_entries` expense rows with method `прочее`: bank commission, tea/cookies, glue/film, fuel, and `Дима(17-24.04)`.
- Used `2026-04-30 12:00:00+03` for all inserted rows to match the existing manual batch for that day.

#### Verified

- Confirmed no matching duplicate rows existed before insertion.
- Verified inserted cashbook ids `721`-`727` are active.
- Verified 2026-04-30 totals after insertion: income `463238.00`, expense `232762.00`, delta `230476.00`.
- Sent summary to `Ракета Деньги`, Telegram message_id `9419`.

#### References

- `cashbook_entries`

---

### 2026-05-08 15:10 - Fixed duplicate order cashbook rows and single chat notify

status: completed
actor: codex
scope: Corrected the post-split-payment cashbook regression in code and production data.

#### Changes

- Updated `bot.py` so order income rows are still written per non-wire payment part, but money-flow chat notification is sent once per order with the aggregated non-wire amount.
- Changed `/db_apply_cash_trigger` to disable the legacy `orders` cash trigger instead of recreating it.
- Soft-deleted duplicate production `cashbook_entries` rows for orders `374`-`380` and repointed `orders.income_tx_id` to surviving rows.
- Disabled the active production `orders` cash trigger and removed its trigger functions.

#### Notes

- Verified no extra active order-income rows remained versus `order_payments` after cleanup.
- Historical legacy `р/с` order-linked cashbook rows were left untouched.

#### References

- `bot.py`
- `cashbook_entries`
- `order_payments`

---

### 2026-05-02 12:27 - Refreshed project context for next work

status: completed
actor: codex
scope: Read the project onboarding and central context files before implementation work.

#### Changes

- No repository code changes.
- Refreshed `AGENT_STATE.md` to note that Google Calendar integration is still future work and current Google code is Sheets-only.

#### Verified

- Read `CLAUDE.md`, infra map, central state/log, `project.md`, Google Calendar technical task, `README.md`, notification rules/modules, `crm/wahelp_service.py`, migration list, and key `bot.py` runtime sections.
- Ran `git status --short`; worktree was clean.

#### Next Steps

- For calendar work, start with schema additions for `calendar_orders`, `order_confirmations`, and `orders.calendar_order_id`.
- Keep changes narrow around the existing manual order flow and notification outbox.

#### References

- `CLAUDE.md`
- `project.md`
- `technical_task_google_calendar_integration.md`
- `bot.py`
- `notifications/`

---

### 2026-05-22 15:40 - Reworked cleaning contour UX, roles, and notifications

status: deployed
actor: codex
scope: Planned, implemented, and deployed the requested cleaning contour rework.

- Added approved design/implementation plan plus commits for cleaner role/menu access, minimum amount, reworked FSM, notifications, lookup, expenses, diagnostics, and `staff.role='cleaner'`.
- `cleaning_foremen` remains profile/order attribution; `staff.role='cleaner'` controls access.
- `Расчётный` records income immediately and does not spend/accrue bonuses.
- Verified with `tests/test_cleaning_helpers.py`, py_compile, production restart, and DB permission checks.

---

### 2026-05-22 11:46 - Cleaned amoCRM alert formatting and duplicate lead/unsorted alerts

status: completed
actor: codex
scope: Fixed noisy amoCRM API alert output after live MAX message testing.

- Commit `e42cad8`: cleaned `new_unsorted` formatting, phone extraction, source labels, and comment categories.
- Commit `5a1dd50`: added cross-dedupe between `lead_added` and linked `unsorted` by `lead_id`.
- Deployed `5a1dd50` to `/opt/telegram-bot` and restarted `telegram-bot.service`.
- Verified parser tests, py_compile, production restart, and polling log state.

---

### 2026-05-21 16:30 - Implemented cleaning foreman + cleaning order flow

status: completed
actor: claude
scope: Built a parallel cleaning financial contour with FSM/admin commands and processed first smoke order in prod.

- Added design docs, migration `0006_cleaning.sql`, `ensure_cleaning_schema()`, and package `cleaning/`.
- Wired `/cleaning_order`, balance/cash/order admin commands, and cancel/admin helpers.
- Fixed global fallback with `StateFilter(None)` so it does not intercept cleaning FSM input.
- Prod deploy reached `ee81ed3`; test foreman `7671577717`; smoke order #1 passed.
- Pending: diagnose delivery to `CLEANING_MONEY_FLOW_CHAT_ID=3687084157`.

---

### 2026-05-22 11:24 - Deployed amoCRM API polling alerts

status: completed
actor: codex
scope: Replaced noisy amoCRM webhook alerts with API polling filtered to the primary processing pipeline.

- Added `notifications/amocrm_api.py`, API cursors, event/unsorted dedupe, and delayed unanswered-message state.
- Enabled polling for lead, chat, and unsorted events filtered to pipeline `4482751` and status `41463535`.
- Disabled production webhook token and enabled amoCRM API env with long-lived token.
- Deployed commit `ee81ed3`; local/prod verification and runtime polling state passed.

---

### 2026-05-21 10:57 - Enriched amoCRM admin alerts

status: completed
actor: codex
scope: Improved amoCRM webhook alert text with fields needed for faster admin reaction.

- Added phone extraction from unsorted source/contact fields.
- Added deal links from payload account URL plus lead id.
- Added comment extraction from order/comment and chat/source text fields.
- Deployed commit `280f787`; smoke POST and DB event handling were confirmed.

---

### 2026-05-21 09:19 - Activated public dastydev amoCRM webhook

status: completed
actor: codex
scope: Finished the public HTTPS endpoint for amoCRM webhook alerts after Timeweb firewall and DNS were corrected.

- Confirmed DNS for `dastydev.ru` and `www.dastydev.ru`.
- Opened tcp/80 and tcp/443 in UFW; existing Let’s Encrypt cert was valid.
- Added nginx HTTPS config for `/amocrm/webhook` to `127.0.0.1:8080`.
- Public smoke POST and DB event row were confirmed; production remained on `1d200d1`.

---

### 2026-05-20 14:51 - Deployed amoCRM webhook code, public hooks domain blocked

status: partial
actor: codex
scope: Deployed amoCRM webhook code and verified local runtime handling; public hooks domain remained blocked by DDoS-Guard.

- Pushed/pulled production to `1d200d1`, added `AMOCRM_WEBHOOK_TOKEN`, and restarted `telegram-bot.service`.
- Added initial nginx HTTP block for `hooks.crmfit.ru`.
- Local production smoke on `127.0.0.1:8080/amocrm/webhook` handled event row `1`.
- Public `hooks.crmfit.ru` returned DDoS-Guard 403; direct HTTP to server IP reached aiohttp but was not the approved final URL.

---

### 2026-05-22 18:25 - Fixed cleaner Telegram menu and button routing

status: deployed
actor: codex
scope: Fixed post-deploy cleaner UX regressions: admin command menu leakage and cleaner buttons falling into fallback.

- Commit `89e5a34`: default bot commands reduced to `start/help/whoami`; role-specific commands now use chat scopes.
- Cleaner command scope includes `cleaning_order`, `cleaning_balance`, and `cleaning_expense`.
- Cleaner reply-keyboard buttons are bridged before fallback; `🔍 Клиент` routes to cleaning lookup for cleaner role.
- Deployed `89e5a34` to `/opt/telegram-bot`; local/prod verification passed.

---

### 2026-05-25 12:10 - Restored Wahelp callbacks and feedback reminders

status: completed
actor: codex
scope: Diagnosed missing feedback messages, restored Wahelp webhook ingress, and repaired stuck notification state.

- Root cause: public tcp/8080 was blocked while the bot listened on `/wahelp/webhook`.
- Added nginx HTTPS proxy `dastydev.ru/wahelp/webhook` to `127.0.0.1:8080`.
- Created the HTTPS Wahelp webhook and removed the old direct `91.200.150.68:8080` webhook.
- Reset stuck `client_channels` and requeued four failed `order_rating_reminder` rows.
- Verified restored reminders sent and fresh Wahelp status callbacks updated `notification_messages`.

---

### 2026-06-03 11:20 - Built and merged read-only money analytics dashboard

status: completed
actor: codex
scope: Designed, planned, implemented, merged, and pushed a separate web dashboard for money analytics.

- Main pushed to `75f3349`; feature worktree removed and branch deleted.
- Added `analytics_app/` aiohttp service with login/password auth, read-only query layer, main cash and cleaning dashboards, charts, and ledger search.
- Added `docs/analytics_deploy.md` for `analytics.dastydev.ru`.
- Verified with 69 unittest tests, analytics py_compile, and fake-data HTTP smoke.
- Follow-up deploy completed later the same day at `https://analytics.dastydev.ru`.

---

### 2026-06-03 15:15 - Deployed money analytics dashboard

status: deployed
actor: codex
scope: Deployed the separate web dashboard for owner/partner money analytics at `analytics.dastydev.ru`.

- Production `/opt/telegram-analytics` is on pushed `main` commit `75f3349`.
- Created `/etc/telegram-analytics.env` with DB DSN, generated session secret, and owner/partner password hashes.
- Installed systemd unit `telegram-analytics.service`; service runs on `127.0.0.1:8090` as `admin`.
- Added nginx HTTPS reverse proxy for `analytics.dastydev.ru` and issued Let's Encrypt certificate expiring 2026-09-01.
- Verified HTTPS `/login` returns 200, owner login returns 302, authenticated `/` and `/cleaning` return expected dashboard HTML, service is active, and `nginx -t` passes.
- Remaining hardening item: replace reused bot DB credentials with a read-only PostgreSQL role for analytics.

---

### 2026-06-04 10:40 - Deployed Analytics v2 management dashboard

status: deployed
actor: codex
scope: Reworked the analytics web app from a cashbook viewer into a management dashboard and deployed it to production.

- Main pushed and production `/opt/telegram-analytics` deployed to `cfe63de`.
- Added arbitrary `date_from`/`date_to` period controls with automatic chart bucket selection.
- Added management metrics from `orders`, `order_payments`, `payroll_items`, cashbook operating expenses, and cleaning tables.
- Dashboard now shows gross checks, live money, salary load, salary percent, bonus loss percent, expense groups, largest expenses, charts, and ledger drill-down.
- Query layer keeps visible ledger capped separately while management expenses use full-period operating expenses; order live money prefers `order_payments`.
- Verified locally with 31 analytics tests, 80 full tests, analytics py_compile, and fake-data HTTP smoke.
- Verified production HTTPS `/login` 200, owner login 302, authenticated `/` and `/cleaning` contain Analytics v2 sections and `management-chart-data`; `telegram-analytics.service` is active and `nginx -t` passes.
- Remaining hardening item: replace reused bot DB credentials with a read-only PostgreSQL role for analytics.

---

### 2026-06-04 10:29 - Fixed analytics live money for linked wire payments

status: deployed
actor: codex
scope: Reconciled May dashboard gap between gross checks and live money.

- Root cause: six May `р/с` orders had real linked income in `cashbook_entries`, but `order_payments` still contained `р/с:1.00` placeholders.
- Commit `7460733` changes main analytics live-money SQL to use non-wire `order_payments` plus linked `р/с` income from `cashbook_entries`.
- Verified May 2026 on production data: checks `263225.00`, live money `256252.00`, bonuses `6973.00`, remaining gap `0.00`.
- Deployed `/opt/telegram-analytics` to `7460733`; service active; `https://analytics.dastydev.ru/login` returns 200.
- Verification: analytics tests, full unittest suite, analytics py_compile.

### 2026-06-04 12:06 - Aligned analytics dashboard with cashbook profit model

status: deployed
actor: codex
scope: Reworked main analytics to separate cashbook profit from order economics and added user-confirmed expense categories.

- Commit `d9be6ea` changes main dashboard top metrics to cashbook income, cashbook expenses, cashbook profit, DIV paid, and cash balance.
- Order block now reports checks, live payments, bonuses, and calculated payroll percentages without using calculated payroll to reduce cashbook profit.
- Expense grouping now includes mappings for salary/contractors, partners, marketing/site, chemistry/consumables, equipment repair, staff gifts, taxes, office, IT/services, bank commissions, logistics, and other.
- Verified May 2026 via local DB tunnel: income `269895.00`, expenses `250275.00`, profit `19620.00`, DIV paid `300000.00`.
- Deployed `/opt/telegram-analytics` to `d9be6ea`; service active; `https://analytics.dastydev.ru/login` returns 200.
- Verification: 32 analytics tests, 81 full tests, analytics py_compile.

---

### 2026-06-04 10:43 - Fixed double-counted payroll payouts in analytics

status: deployed
actor: codex
scope: Excluded salary payout cashbook rows from management `Прочие расходы`.

- Root cause: `Зп...` cashbook expense rows were included in `Прочие расходы` while salary was already counted from `payroll_items`.
- Commit `4c22b3c` adds payroll-payout detection and excludes those rows from operating expenses.
- Verified May 2026 production data: old other expenses `250275.00`, excluded salary payouts `131370.00`, new other expenses `118905.00`, operating profit `43062.00`.
- Deployed `/opt/telegram-analytics` to `4c22b3c`; service active; `https://analytics.dastydev.ru/login` returns 200.
- Verification: 32 analytics tests, 81 full tests, analytics py_compile.

---

### 2026-06-04 15:11 - Fixed analytics charts and GSM categorization

status: deployed
actor: codex
scope: Fixed blank dashboard chart panels and prioritized GSM expense classification.

- Root cause for blank charts: JSON payloads inside `<script type="application/json">` were HTML-escaped as `&quot;`, so `JSON.parse` failed before Chart.js could render.
- Commit `75fa6d0` renders safe raw JSON for chart payload scripts and adds a regression test that parses `management-chart-data`.
- Added `ГСМ` priority so comments such as `ГСМ теплоходы` classify as `Транспорт/логистика`.
- Deployed `/opt/telegram-analytics` to `75fa6d0`; service active; `https://analytics.dastydev.ru/login` returns 200.
- Verification: 32 analytics tests, 81 full tests, analytics py_compile.


---

### 2026-06-04 16:20 - Added expense categories to bot cashbook flow

status: deployed
actor: codex
scope: Persisted agreed expense categories for newly recorded main cashbook expenses.

- Commit `559fb5d` adds shared expense category rules, `cashbook_entries.category`, and migration `0007_cashbook_expense_category.sql`.
- Main expense FSM now suggests a category after the comment, supports manual category selection, shows category at confirmation, and stores it on confirm.
- `/expense` now auto-detects category from the comment and stores it with the cashbook row.
- Analytics prefers stored category for new rows and keeps heuristic grouping for historical rows.
- Deployed `/opt/telegram-bot` and `/opt/telegram-analytics` to `559fb5d`; both services active; analytics `/login` returns HTTP 200.
- Verification: 34 analytics tests, 83 full tests, py_compile for analytics, bot, and shared category module; production DB has `cashbook_entries.category:text`.

### 2026-07-08 12:04 - Disabled unused client rewash follow-up

status: deployed
actor: claude
scope: Investigated a new ops-chat alert for order #458 and disabled the noisy, unused client rewash follow-up job.

- Alert `Не удалось отправить follow-up по перемыву #458` came from `run_rewash_followup_job`: client blocked the client bot, so the "reply 1/2" follow-up failed and posted to the ops chat.
- Root cause: on a successful send the job rescheduled +24h without advancing `rewash_cycle`, so it re-sent daily forever; on client block it retried every 2h and spammed the ops chat.
- `rewash_result` is never written anywhere in the repo — no client-reply handler exists, so the follow-up loop could never resolve by answer.
- Decision (client feature unused): disable only the client follow-up; keep master rewash marking and `rewash_counter_task` (5/month alert).
- Commit `3029015` comments out the `rewash_followup_task` scheduler registration; job/function left dormant with a revive note.
- Cleared order #458 in prod DB (`rewash_followup_scheduled_at = NULL`) to stop the active loop before the fix.
- Deployed `/opt/telegram-bot` to `3029015`; `telegram-bot.service` restarted and active; startup shows `rewash_counter` scheduled and no `rewash_followup` job.

deploy SHA: 3029015

### 2026-08-12 14:48 - Reworked client texts, contact footer and dead-code cleanup

status: deployed
actor: claude
scope: Shortened service texts, replaced client-bot link with MAX/TG chat contacts, removed dead code, updated the Google Calendar task.

- `17e0a16`: order summary 10 lines -> 5, rating request 3 lines -> 2, markdown `**` removed (they reached clients literally: bot sends without parse_mode).
- `ea18c67`: WA footer now `Мы всегда на связи здесь:` + MAX/TG chat; links moved to env `WA_CONTACT_MAX_LINK` / `WA_CONTACT_TG_LINK`.
- `688e1d7`: footer no longer WhatsApp-only — glued in all channels; cleaning events added to `SERVICE_FOOTER_KEYS`.
- `8151325`: removed promo stage 2 call, `{TG_LINK}` substitution and the whole rewash client follow-up (143 lines); rewash marking and 5/month counter kept.
- `9b17a97`: Google Calendar task updated to v2 — cleaning contour, `Заказы сегодня` button, contour by role, one card = one order per contour, `да`/`нет` confirmation, no second reminder, admins do not complete orders.
- Outside repo: fixed the n8n text generator (owner applied it) — MAX link updated, promo footer moved from prompt into the code node.
- Findings: 11 texts in `notification_rules.json` are never sent (Google Calendar groundwork, keep); promo selection is a 195-day carousel, all 3905 candidates already received it; owner decided to keep it as is.
- Year stats: 520 orders, 454 clients, 3 535 590 ₽; 91.6% ordered once. After promo: 107 clients, 883 545 ₽ (25% of revenue); response `1` converts at 82%.
- Backups verified: nightly 03:40, local + offsite to contabo_nl, 15 copies each, 14-day retention, today's copy present.

deploy SHA: 8151325

---

### 2026-08-24 18:30 - amoCRM automation recon (coworker brief) completed

status: done (no code/prod changes)
actor: claude
scope: Read-only recon of amoCRM, bot code/DB, Google Calendar, orders chat, partner Excel; all matching metrics computed; reports written to tgbot-v1/recon/ (uncommitted by owner decision).

- amo structure dumped via bot token (read-only): 7 pipelines, 65 lead fields (53 dead), 3 users, 1 webhook; autotasks by robot confirmed from 897 tasks/90d.
- Metrics: calendar->amo 87% auto (M1 70 + M2 17), bot->amo 92%, partner Excel->amo 90-96%, phone extract from calendar 98.5%, contact dups 1.4%, M9 weekday-only dates 0% in texts (but present in note screenshots).
- Facts vs brief: salesbot creates 2nd deal per order; cancellation = calendar event deletion (no "отменен" comments); chat photos = calendar screenshots; carpet orders dictated to partner by phone, chat is internal-only; "Ковры Кристал" = partner Kristall; amo token read-only.
- Bot risk logged: 5 phone-normalization families; canonical char-scan (bot.py:3251) must become the standard for matching.
- Calendar raketaclean52@gmail.com shared read-only to copypast.pe@gmail.com; 462 events pulled (6 mo).
- Owner decisions: roptick excluded; recon/ stays local uncommitted; recon/data/ (PII) gitignored.
- Next: architecture phase on top of recon/00-summary.md; will need amo integration with write scope.

deploy SHA: 8151325 (unchanged)

---

### 2026-08-13 10:55 - Post-deploy verification of 2026-08-12 changes

status: verified
actor: claude
scope: Checked logs, deliveries, nightly jobs, n8n output and backups after the 2026-08-12 deploy. No code changes.

- `telegram-bot.service` up on `8151325` for 20h, 0 restarts, 0 error-level entries, 0 tracebacks.
- 3 `order_completed_summary` messages delivered with the new short text (2 WhatsApp, 1 Telegram); 3 `order_rating_reminder` queued for +24h.
- Contact footer confirmed on prod by calling `_with_contact_footer` in the prod venv: `send_with_rules` applies it, links correct.
- `rewash_counter` ran 10:00 MSK — the kept part of the rewash code survived the 143-line deletion.
- n8n overnight batch (13.08 09:00 UTC): promo 10/10 and birthday 5/5 carry the new MAX link and TG chat, zero old bot links, zero `⚠️`.
- Backup 03:40 created and replicated offsite; both copies present.
- Note for future checks: `notification_messages.message_text` stores the text BEFORE the footer is glued, and journald truncates the send log after the second line — neither proves what the client received.
- `promo_reminders` / `birthday_bonuses` had not run yet at check time (scheduled ~11:00 / ~12:00 MSK); first run of the promo code without stage 2 not yet observed.

deploy SHA: 8151325 (unchanged)

---

### 2026-08-24 21:40 - Designed amo-sync v1 and new raketa-admin-bot service

status: design approved
actor: claude
scope: Brainstorming (superpowers skill) with owner on top of finished recon; design doc written and committed in NEW repo raketa-admin-bot. No tgbot-v1 changes.

- Owner decisions: separate admin-bot service (future platform for owner-only admin functions), NOT a module of prod bot; v1 = full amo deal cycle driven by bot DB (find/create lead -> Передано в работу -> salesbot autodeal -> ЗАКАЗ ВЫПОЛНЕН и Оплата получена); calendar = phase 2, carpets Excel = phase 3, CSV import replacement = phase 4.
- Behavior: auto when unambiguous, TG card with buttons when not; instant processing + 21:00 MSK reconciliation summary; close amo autotasks; close "Получить ОС" if client already rated in bot; Услуга default by master (Никита/Дима=мебель, Оля=клининг); budget = full check.
- Safety: read-only on bot DB (own schema for state), new amo integration with write scope, per-feature kill switch, idempotent step checklist, backlog from 2026-08-21 runs with dry-run preview + "Поехали" button.
- New repo: ~/Projects/raketa-admin-bot, design at docs/plans/2026-08-24-amo-sync-design.md (commit 2ac4614).
- Next session: invoke superpowers:writing-plans for implementation plan (session task #6).

deploy SHA: 8151325 (unchanged)

---

### 2026-08-27 12:40 - Cleaning contour put into production for Olga

status: done (prod data + env change, no code changes)
actor: claude
scope: Onboarded the real cleaner, wiped test cleaning data, entered the opening cash balance, fixed the cleaning money-flow chat id.

- Cleaner `tg=5195851358` renamed from «Клинер» to **Ольга Скоропашкина, +79081572721** in both `staff` and `cleaning_foremen`; role `cleaner` and command menu already in place, no restart needed for that.
- Test foreman `tg=7671577717` («Тест Бригадир») intentionally kept active — owner uses it himself.
- Test cleaning order #1 (10 000 ₽, 21.05) cancelled by SQL equivalent of `/cleaning_cancel_order`: order + 3 cashbook rows soft-deleted, mirror bonus rows written, test client 5108 back to 800 bonuses. Cleaning cash went 4 700 → 0.
- Opening balance entered by owner via `/cleaning_cash_add`: `deposit` «Наличные» 3 793 ₽. Deposit is excluded from P&L by design, so it lifts the balance without touching profit.
- **Env change:** `CLEANING_MONEY_FLOW_CHAT_ID` `-3687084157` → `-1003687084157` (group had become a supergroup, old id gave `Bad Request: chat not found`). Backup at `/opt/telegram-bot/.env.bak-20260827`; service restarted; delivery confirmed by owner with a 1 ₽ test deposit, which was then deleted.
- Final state: cleaning cash 3 793 ₽, one cashbook row, zero active cleaning orders, money-flow alerts working.
- One-page Russian instruction sent to Olga's private bot chat (owner-approved text, message 39637).
- Diagnostic finding: on the VPS DNS returns an unreachable address for `api.telegram.org` (IPv6 plus IPv4 `149.154.166.110`); the working path is `curl --resolve api.telegram.org:443:149.154.167.220`, which is how the bot itself is connected.
- Open items for owner: whether Olga needs `cleaning_view_reports`; whether the 5 500 ₽ minimum fits real cleaning jobs; optional one-page instruction for Olga.

deploy SHA: 8151325 (unchanged)

---

### 2026-08-28 - amoCRM auto-exchange built; client messaging designed and deferred

status: code complete, not deployed
actor: claude
scope: tgbot-v1, branch `feature/amo-exchange` at `b96c291` (pushed). 108 tests green, 25 new.

- **Why**: the owner exported deals to CSV weekly by hand, and the import silently
  lost data — it looks up columns by name, and the export template in CRM had
  changed, so name, bonuses and birthday never arrived while the import reported
  success. Checked on his real file: 62 deals, 58 phones, and 3 refusals would have
  become clients under the old address-based rule.
- Built `notifications/amo_exchange.py` (pure rules + amo parsing, 25 tests) and a
  thin layer in `bot.py`: poller with its own cursor (`exchange_events`), event
  dedup, reading and writing `clients`, switch plus rehearsal mode, daily summary
  counted from the event journal so it survives restarts.
- Owner's rules: fills address, service and empty name only. Never bonuses,
  birthday, district or last order date — they are born in the bot. **A client is
  never demoted to lead**; a lost deal of a known person changes nothing.
- A live check changed the design: `lead_status_changed` events carry the new status
  and pipeline, so foreign deals (hundreds a day) are filtered without fetching each
  card. The decision comes from the event, not the card — a deal moved further still
  means the order was placed.
- **Deployed to prod in rehearsal** the same evening. Two bugs caught there, not by
  tests: the rehearsal shared its cursor and event journal with the live run (a day
  of rehearsal would have made the live run start from «nothing changed» — the same
  mistake that ate the partner's letters on 2026-08-26), and the last event was
  recounted every pass, filling the log.
- Owner's idea, implemented the same evening: **two passes at different speeds** —
  won every 10 minutes on its own schedule, lost in one batch on Mondays at 10:00.
  Nobody messages a refusal, so a weekly batch is easier to check than a trickle.
  The poll stays as insurance for deals booked past the robot.
- Rehearsal on real data: 4 deals of the day, 3 clients unknown to the bot's
  database — exactly the gap the exchange closes.
- Client messaging: design plus a 7-task plan written
  (`docs/plans/2026-08-28-client-messaging-*.md`), texts approved by the owner.
  Not started; blocked until the exchange runs live.
- Deploy SHA: `ff95fc8` (branch `feature/amo-exchange`, rehearsal mode).

---

### 2026-08-28 - client messaging before the job built end to end (tasks 1-7)

status: superseded the same evening by the entry above
actor: claude
scope: tgbot-v1, branch `feature/amo-exchange` at `2fb65b2` (pushed). 149 tests green, 39 new.

- Built all seven tasks of `docs/plans/2026-08-28-client-messaging-implementation.md`:
  `order_confirmations` table, pure rules in `notifications/client_messaging.py`,
  both letter texts, poller of «Заказ оформлен», answer parsing in
  `handle_wahelp_inbound`, silence watch, switch + rehearsal + daily summary.
- **First write to amoCRM in this bot** (`patch` / `update_lead_status`): the client
  could only read until now. Whether the prod token may write is unverified; a denied
  write raises and the owner is told — silence would leave him sure the CRM matched.
- Two deviations from the plan, both deliberate: «не» dropped from the refusal list
  («не знаю» is a conversation, not a refusal, and would have inflated the refusal
  count in his report); and no `asked` status — rows stay `planned` with `asked_at`
  holding the planned question time, because the owner ruled the reason for silence
  does not matter.
- Branch order matters: явные «да»/«нет» go to confirmation first, anything unclear
  is offered to the older branches (rating, STOP, promo) before the owner — otherwise
  a «5» from a client awaiting confirmation would be lost as an unclear answer.
- Rehearsal leaves no traces (cursor in memory, own journal key, no rows, no letters),
  so the live run starts clean. Cost: answer parsing and the CRM write get their first
  real proof only in live mode.
- Also closed a plan warning by fact: the rules path is hardcoded, and
  `docs/notification_rules.json.local` is read by nobody.
- Not deployed by design. Next: `scripts/check_order_created_events.py` on the server,
  then deploy, then `CLIENT_MESSAGING_ENABLED=1` with `DRY_RUN=1`.

deploy SHA: unchanged (`ff95fc8` on prod)

---

## Из журнала raketa-admin-bot

### 2026-09-03 - waiting for an answer is switched on by the question, not by the schedule

status: deployed to prod, one alert already fired on live data
actor: claude
scope: tgbot-v1, `feature/amo-exchange`, commits `6819870`..`574b5ee` (pushed), prod `574b5ee`. 213 tests, 25 new.

- A hotel client wrote about his own business five days before the robot planned to ask
  anything, and the robot took it for an answer. Waiting was created when the order was
  placed; the question goes out a day before the work; everything in between counted.
  A plain «да, спасибо» would have confirmed a deal nobody asked about.
- Fix 1: `asked_sent_at` - the fact of sending, apart from the planned `asked_at`.
  Waiting is live only after it; the silence watch counts from it. Old rows backfilled
  from `notification_outbox.sent_at`. The silence index had to be **renamed**:
  `CREATE INDEX IF NOT EXISTS` matches by name, not by columns.
- Fix 2: one second before the question the robot checks amoCRM (deal exists,
  realization pipeline, «Заказ оформлен»); otherwise the letter is cancelled and waiting
  gets status `dropped`. The queue worker got two hooks and still knows nothing about CRM.
- Fix 3 (owner's decision): a question that never reached the client calls the owner.
  Live data corrected the rule the same hour - a letter cancelled with «client never
  wrote to us first» counts too, and it fired for real on deploy (order for 04.09).
- Another session works the cleaning contour in the same tree; its commits are pushed,
  not deployed, and not mixed into these.

deploy SHA: `574b5ee`

---

_Прежняя шапка: «Записи, ушедшие из SESSION_LOG.md по правилу «последние 10». Только дополняется.»_

### 2026-09-03 - Трафик до Telegram переведён на прокси вне РФ

status: развёрнуто, все функции в бою
actor: claude
scope: ветка `feature/amo-sync-v1` (HEAD `2b6c0ee`, запушена), служба перезапущена 13:22 МСК. 735 тестов зелёных, 6 новых.

- Блокировка Telegram на российском сервере идёт волнами и гасит **все** прямые адреса
  разом. Замер на рабочем боте: из 30 запросов 4 висели по 10 секунд, живым был один адрес
  из пяти. Через голландский сервер — 30 из 30, ни одного дольше секунды. Владелец решил
  перевести все три бота; устройство и замеры: `tgbot-v1/docs/plans/2026-09-03-telegram-proxy.md`.
- Сессия умела прокси и раньше, но настройку ей никто не передавал: `build_session`
  вызывался без второго аргумента. Добавлено поле `telegram_proxy_url`, чтение
  `TELEGRAM_PROXY_URL`, передача в обоих местах создания ботов.
- Включение штатным ключом, а не правкой `.env` (он доступен только root):
  `--telegram-proxy=URL`, откат `--telegram-proxy-off`. Адрес не печатается — в нём пароль.
- Заодно починен `set_flag`: значение подставлялось `sed` с косой чертой в роли
  разделителя, а в адресе прокси косые черты есть — команда разорвалась бы.
- **Своя же ошибка, найдена и исправлена в тот же заход**: строка ссылалась на
  необъявленную переменную, а скрипт работает с `set -u`, и обычный
  `sudo raketa-admin-bot-update` падал на шаге 5, до перезапуска. Поймано тем, что в
  выводе не оказалось добавленной строки про маршрут; обычный деплой перепроверен.
- На живом сервере: 0 прямых соединений с Telegram, все идут через Голландию.

deploy: `2b6c0ee`, служба active

---

### 2026-09-02 (вечер) - Загадка переадресации разгадана, autocall в бою

status: completed; фича БОЕВАЯ, живая заявка прошла весь путь
actor: claude (параллельно шла отдельная сессия по календарю и почте владельца)
scope: raketa-admin-bot, feature/amo-sync-v1, 6 коммитов, pushed, выкачено. 729 тестов.

- **Причина найдена**: правила переадресации внутреннего номера действуют
  только на входящих, идущих по схеме АТС; звонок роботом отдаётся командой
  API и их не видит. 4 прогона через внутренний 100 — цепочка не поднялась
  ни разу; в двух «удачных» менеджер просто успел взять трубку в приложении.
- Решение: робот звонит на мобильный админа, повтор через 5 минут — на его
  личный (`PBX_MANAGER_DIAL=рабочий,личный`, ключ `--manager-dials=`).
- **Стенд из трёх прогонов дал образцы всех исходов.** Прежнее правило было
  неверным и опасным: «менеджер не взял» засчитывалось как вина клиента, и
  сделка после второго раза уехала бы в «Не было 1-го касания».
- Таймаут команды звонка оказался штатным признаком «менеджер не снял
  трубку»: АТС держит ответ до его ответа. Прошлая загадка закрыта.
- Отбивка заменена признаком владельца: телефон звонит, приложение молчит.
- **Живая заявка 18:08**: сделка → звонок → админ снял за 9 с → соединение →
  верный исход → сообщение владельцу. Всё быстро и чётко.
- **Новая дыра**: звонок не попал в амо — связка знает только внутренний 100.
  Робот теперь пишет в сделку примечание сам; вопрос в поддержку onlinePBX
  подготовлен, отправляет владелец.
- Deploy SHA: `38e90fc`, служба перезапущена 18:22, autocall БОЕВОЙ.

---### 2026-09-02 (день) - Дата из календаря правит сделку; почта с повтором; чистка CRM

status: completed; всё выкачено, служба работает
actor: claude (параллельно шла отдельная сессия по autocall — её запись отдельно)
scope: raketa-admin-bot, feature/amo-sync-v1, 7 коммитов, pushed. 755 тестов зелёных.

- **Потерянное уведомление найдено**: 12:41 робот привязал запись к сделке 31568495,
  12:42 отчёт не ушёл — таймаут Telegram, повтора не было by design (завершённые
  записи не перебирались). Лечение двойное: почта владельца (миграция 009,
  `adminbot/tg/outbox.py` — все сообщения через одну дверь, недоставленное лежит
  долгом и досылается, отметку ставит сама почта) и добор завершённых записей без
  отчёта за трое суток. В `/status` строка «Жду отправки: N».
- **День работы берётся из календаря** (отменяет решение 5 от 26.08). Правило пряталось
  в трёх местах: «дата только в пустое поле», выброс поля из правки записи, order_date
  не считался изменяемым полем. Перенос = смена ДНЯ; время внутри дня — владельца.
- **Забытые сделки не мешают работе.** Хвост старше полугода — повод завести новую
  и сказать строкой в отчёте, а не спросить. На кнопке у сделки не этого года
  появился год (владелец принял «11.09» 2024-го за свежую дату).
- **Чистка CRM (разовая, по решению владельца)**: команды `--stale` (замер) и
  `--close-stale[-live]` (просмотр/закрытие, три предохранителя). Было 94 незакрытых,
  60 забытых. Закрыто 60: заведённые до 2025 — «не реализовано» (46 шт., 192 212 ₽
  в потери), с 2025 — «успешно» (14 шт., 26 549 ₽ в выручку), в каждой примечание
  робота. Осталось 34 сделки, все рабочие; забытых — 0.
- Deploy SHA: `71ab996` (мой последний); служба перезапускалась четырежды.

---

### 2026-09-02 - Переезд записи ≠ отмена; смешанную работу делит владелец, не робот

status: completed; выкачено в бой, служба перезапущена 11:51 МСК
actor: claude (параллельно шла отдельная сессия по autocall — её запись ниже)
scope: raketa-admin-bot, feature/amo-sync-v1 → `c905a05`, pushed. 697 тестов.

- Владелец разобрал четыре сценария ведения календарей. Три из них уже работали;
  чинили первый: завёл заказ не в тот календарь, перенёс в правильный.
- **Переезд больше не считается отменой.** Для покинутого календаря Google отдаёт
  то же самое, что при удалении, и 2026-09-01 робот так закрыл две живые записи.
  Теперь перед отменой он спрашивает остальные календари (`get_event` по id —
  при переносе id сохраняется). Нашлась — ведём как обычную запись, сделка та же.
- **Сбой связи не считается «записи нигде нет»**: обмен по календарю прерывается,
  закладка не двигается, удаление придёт следующим проходом. Цена ошибки
  несимметрична — лишний проход стоит 5 минут, закрытая сделка не откатывается.
- **Отклонена доработка «одна запись → две сделки»** (сценарий 4). Владелец будет
  заводить смешанную работу двумя записями. Разделение стоило бы дня (ключ
  «одна запись — одна сделка» лежит в схеме БД), а две сделки нужны в любом
  случае: в боте уборку и мебель закрывают разными заказами, а сделка
  обслуживает один заказ. Оба правила записаны в `docs/gcal_access.md`.
- Проверено на живом примере (сделка 31513559, бюджет 55 000 = 35 000 + 20 000):
  поле «Услуга» мультиселект и робот пишет список; «Специалиста» по календарю
  он не ставит вовсе — тот приезжает из закрытого в боте заказа.
- В сессию попали чужие задачи, разнесены по проектам: разбор «не работает VPN» →
  `project_ai_context/vpn/`; «изъятие денег из кассы в боте клинера» → `tgbot-v1`.
- Deploy SHA: `c905a05`; autocall не трогали, он остался в репетиции.

---### 2026-09-02 - Стенд autocall: отбивка невозможна, боевой старт отложен

status: paused by owner; репетиция продолжает работать, хвост — в AGENT_STATE
actor: claude
scope: raketa-admin-bot; код не менялся (1 коммит в docs), тесты не трогались.

- Автоответ салесбота: владелец заменил на универсальный короткий текст
  (химчистка+уборка, 10:00–20:00, «позвоним в ближайшее время»).
- Отбивка записана (3,7 с, /home/admin/autocall_prompt.mp3), но НЕ подключена:
  call/now.json аудио не проигрывает, модуль «Приветствие» ломает
  переадресацию — официальный ответ поддержки onlinePBX. Замена (не
  утверждена): TG-сообщение менеджеру до звонка + номер клиента в приложении.
- Стенд, 5 прогонов: менеджер отвечает и переводится; «менеджер не взял»
  надёжно отличим по событиям истории (нет answered_stamp/transfer). Образца
  «настоящее соединение» нет; outcome_from_history — черновик на
  user_talk_time (равен 0 даже при ответе) — переписать на события до боя.
- Урок: таймаут HTTP на call/now не значит «не позвонил» — звонок уходил
  дважды при оборванном ответе; дизайн «намерение до звонка» подтверждён.
- Загадка: в двух прогонах переадресация 100→мобильные не сработала (звонило
  только приложение, 59 с). Гипотеза «приложение перехватывает» отвергнута
  владельцем (5 лет: переадресация работает всегда). Вопрос — в поддержке.
- Решение владельца: сессию закрыть, хвост разобрать в следующей; боевой
  режим autocall не включать до разгадки переадресации.
- Deploy SHA: без изменений; служба живёт, autocall в репетиции.

---### 2026-09-01 (вечер) - Проверка обменов; ковры починены; робот читает два календаря

status: completed; календарь бригадира подключён, отчёт партнёра за август проведён
actor: claude
scope: raketa-admin-bot, feature/amo-sync-v1, 4 коммита, pushed. 689 тестов.

- Проверка по просьбе владельца: заказы (№599–605) и календарь работают,
  системных ошибок нет; обрывы связи с Telegram регулярны, восстанавливаются.
- **Ковры молчали не из-за робота**: письмо партнёра лежало во «Входящих», а он
  смотрит папку `robot_amo`; правила раскладки в почте не было. Владелец завёл
  правило и переложил письмо → в 20:17 робот провёл 10 новых заказов (5 из 15
  строк были сделаны 26.08), по одному спросил владельца. Все 15 — `done`.
- **Календарей стало два** (решение владельца): уборки ведёт бригадир в своём
  календаре «Клининг Оля». Миграция 008 переводит закладку обмена на ключ
  календаря — прежняя таблица допускала ровно одну строку; старая закладка
  достаётся первому календарю из настроек, чтобы не словить полную перезагрузку.
  Наблюдатель обменивается с каждым календарём по очереди, незавершённое
  доделывает один раз за проход, сбой одного календаря не отменяет остальные.
  Новая команда `--gcal-calendars=A,B` (без пробелов — иначе рвётся перенос настроек).
- Разбор записей бригадира правок не потребовал: услуги (включая «Уборка + Мебель»),
  районы, телефоны, адреса — верно на всех живых записях.
- **Найден побочный эффект**: перенос записи между календарями Google неотличим от
  удаления, и робот 01.09 закрыл две перенесённые записи как отменённые. По записи
  «Ниже! Уборка + Мебель Влад» (03.09) сделки в CRM нет — владелец ведёт её сам.
- Вечерняя сводка 21:00 не дошла: таймаут Telegram, повтора отправки нет.
- Deploy SHA: `1ab4314`; служба перезапущена 20:58, autocall остался в репетиции.

---### 2026-08-31/09-01 - Autocall построен за сессию и запущен в репетицию

status: completed; rehearsal live, боевой старт после стенда 2026-09-02
actor: claude (субагентный конвейер: исполнитель + ревью на задачу)
scope: raketa-admin-bot, feature/amo-sync-v1, ~20 коммитов, pushed. 643 теста.

- Новая фича autocall: автозвонок по заявке с сайта. Дизайн и план утверждены
  владельцем 2026-08-31, построены все 12 задач: конфиг, миграция 007,
  окно 10:00–20:00 МСК, машина попыток (5 мин/10 мин/лимит 4), чтение амо
  по тегу «Заявка с сайта», движок, наблюдатель (опрос 30 с), карточки,
  клиент api2.onlinepbx.ru, сборка, флаги --autocall-*, раздел в deploy.md.
- Экзамен на живой CRM: 62 заявки/60 дней, 0 ложных, задержка письмо→сделка
  25–34 с. Теги «Сайт»/«карты» — входящие звонки, им не звоним (владелец).
- Ревью поймало два серьёзных бага до боя: повторный зачёт старого исхода
  из-за непотёртого call_id при повторе; зомби-цепочка без телефона.
- Ключ onlinePBX переиспользован из env рабочего бота → /home/admin/.pbx.env;
  АТС сама отдала внутренний 100 «Амо» (цепочка менеджера) и чат менеджера.
- Код доезжал на VPS без перезапуска службы (диагностический путь update.sh);
  единственный перезапуск — включение репетиции 2026-09-01 14:17 МСК.
  Владелец проверил на живых заявках, тексты менеджеру утвердил.
- Урок владельца: субагентам всегда задавать подходящую модель, не топовую.
- Найдены правдоподобные ПД в старых тест-фикстурах — частично заменены,
  сквозная чистка — отдельная задача; история git — решение владельца.
- Deploy: сервис на коде HEAD, autocall в репетиции, остальные фичи БОЕВЫЕ.

---### 2026-08-28 (продолжение) - Хвосты разобраны, работа перенесена в tgbot-v1

status: completed
actor: claude
scope: raketa-admin-bot без изменений кода; вся новая работа — в `tgbot-v1`.

- Разобраны хвосты с владельцем. Закрыты: районы (мастера пишут канон),
  названия сделок (остаются за салесботом), Балахнинский и
  Дальнеконстантиновский (владелец добавляет в амо сам). Ждём конца месяца:
  отказы партнёра и месячный свод.
- **Решение по архитектуре**: разговор с клиентом и обмен amoCRM ↔ база строятся
  в `tgbot-v1`, а не здесь. Причина: ответы клиентов приходят на Wahelp-вебхук
  рабочего бота, и только он вправе писать в `clients`. Запрет админ-боту писать
  в чужую базу остаётся в силе.
- Разобран ручной CSV-обмен владельца: импорт молча терял имя, бонусы и день
  рождения — ищет колонки по названиям, а шаблон выгрузки в CRM изменился.
  Проверено на его файле: 62 сделки, 58 телефонов, 3 отказа стали бы клиентами.
- Планы и код по обеим фичам — в `tgbot-v1`, см. его SESSION_LOG за эту дату.
- Deploy SHA: без изменений, `7f55e3a`.

---

### 2026-08-25 - Implementation plan written; repo published; docs committed

status: completed
actor: claude
scope: raketa-admin-bot planning session (with tgbot-v1 and agent1 doc commits).

- tgbot-v1: recon reports committed and pushed (`25ad70f`), recon/data stays gitignored.
- raketa-admin-bot scaffolded from agent1 templates (CLAUDE.md central mode, README, .gitignore) — `31921aa`.
- Implementation plan `docs/plans/2026-08-25-amo-sync-implementation.md` — `0a81e3d`: 15 tasks / 5 phases, TDD, matcher edge cases from recon as tests, history-exam gate (>=92% auto, 0 confident-wrong), dry-run rehearsal, backlog from 2026-08-21, deploy runbook task.
- GitHub repo created and pushed: https://github.com/copypastpe-bot/raketa-admin-bot (private), main at `0a81e3d`.
- Owner decision: execute the plan in a NEW session (executing-plans) in the raketa-admin-bot repo; owner reviews the plan first.
- Deploy SHA: not applicable.

---

### 2026-08-25 - Project scaffolded from agent1 templates

status: completed
actor: claude
scope: Created repo service files and central context; design was approved 2026-08-24 (see tgbot-v1 session log for recon and brainstorming history).

- Repo `~/Projects/raketa-admin-bot` initialized 2026-08-24 with approved design `docs/plans/2026-08-24-amo-sync-design.md` (commit 2ac4614).
- Added CLAUDE.md (context_mode: central), README.md, .gitignore (PII and env excluded).
- Central context created: AGENT_STATE.md + SESSION_LOG.md in agent1/project_ai_context/raketa-admin-bot/; registry.yaml entry and project card projects/raketa-admin-bot.md added.
- Recon reports committed to tgbot-v1 (`25ad70f`, pushed): recon/*.md without data/.
- Next: implementation plan via writing-plans skill, same session.
- Deploy SHA: not applicable (no deploy yet).

---

### 2026-08-25 - Tasks 1-9 built; history exam passed; two real deals processed

status: completed
actor: claude
scope: raketa-admin-bot, branch `feature/amo-sync-v1` (29 commits, pushed). 189 tests green.

- Built tasks 1-9: config, `adminbot` schema + db layer, phone parser, amo read+write
  client (dry-run by default), matcher, checklist, engine, exam and run scripts.
- History exam passed on 457 orders over three 90-day periods: 0 confidently-wrong.
  Untouched period 180-270d — 99.3% auto (guards against rules being tuned to data).
- Exam ran from the VPS: amoCRM was reachable only from Russia that day.
- Found and fixed: amo silently ignores `filter[contacts][id]` and returns all account
  deals; «Дата и время заказа» is sometimes wrong; «Специалист» is the planned master,
  not the actual one; owner processes CRM in batches (deals created after the order);
  regulars book 3 weeks ahead; one deal cannot serve two orders.
- Owner created amo integration with write rights; token on VPS at `~/.amo_write.env`.
- **Two real deals processed live**: orders №585 and №581, full chain through the
  salesbot autodeal. Owner verified both in CRM.
- After owner review added: «Специалист», «Вариант оплаты», «Тип клиента», «Дата оплаты»,
  contact rename, robot's note in the deal, source «Сарафан»/«Повторный заказ».
  New rule: unpaid wire order stops at «Заказ выполнен» so the salesbot asks for payment.
- Deploy SHA: not applicable (no deploy yet; VPS copy is a working dir, not a service).

---

### 2026-08-25 - Tasks 13-14 done; matcher bug found on live data and fixed

status: completed
actor: claude
scope: raketa-admin-bot, branch `feature/amo-sync-v1` at `e449025` (pushed). 272 tests green.

- Task 13: read the live amoCRM field dictionary from the VPS (`scripts/show_field_enums.py`).
  All constants confirmed — Услуга (Чистка мебели 933165, Уборка 772345), Вариант оплаты,
  Тип клиента, Источник сделки. Master→service mapping moved from code to `SERVICE_BY_MASTER`.
- Task 14: `docs/deploy.md` and `deploy/adminbot.service`. Own system user, GitHub deploy key,
  DB role with SELECT-only on `public` plus its own schema, three migrations, first start with
  the feature switched off. Startup failures now print plain-language hints.
- **Bug found by the rehearsal run, not by tests**: order №587 (25.08, 5 400 ₽) was bound as
  «already done» to deal #31511203 — the same client's earlier order №566 (15.08, 9 050 ₽),
  closed on 25.08 because the owner clears CRM in batches. Order №587 would have silently
  ended up with no deal at all.
- Fix: closing date only counts when «Дата и время заказа» says nothing credible (0-30 days
  between order date and closing). Re-exam on 171 orders: 99.4% auto, 0 confidently-wrong —
  unchanged; №421 (field lies) still resolves automatically.
- Deploy not performed: needs owner's BotFather token and sudo on the VPS.
- Deploy SHA: not applicable.

---

### 2026-08-25 - Tasks 10-12: watcher, owner bot, cards; 259 tests green

status: completed
actor: claude
scope: raketa-admin-bot, branch `feature/amo-sync-v1` (33 commits). Local Postgres used, so DB tests ran instead of being skipped.

- Task 10: `sync/watcher.py` (per-minute loop, per-order isolation, one-time question
  card with retry on send failure) and `sync/reconcile.py` (21:00 MSK summary:
  processed / created / waiting / stuck / missed). Two narrow SQL queries so tick
  cost does not grow with backlog age.
- Task 11: `control.py` (service switch + owner pause stored in DB, migration 002),
  `tg/bot.py` (owner-only guard, /status /backlog /pause /resume /help in plain
  Russian), `main.py` (watcher + reconcile + polling, clean shutdown).
- Task 12: `tg/cards.py` (question card with deal buttons, evening summary, backlog
  preview), `sync/backlog.py` (rehearsal preview → «Поехали» live run), migration 003
  stores question options next to the order.
- Design decision: in rehearsal the service keeps link state in memory — writing
  rehearsal checklist marks to the DB would make a later live run skip real work.
- Fixed: engine's scratch dicts were class-level, so several engines in one process
  shared them; live run could have written to CRM from the rehearsal's calculation.
- Deploy SHA: not applicable (no deploy yet).

---

### 2026-08-25 - Deploy prepared and paused; Telegram reachability solved

status: in progress (owner left; continues 2026-08-26)
actor: claude
scope: raketa-admin-bot, branch `feature/amo-sync-v1` at `6da2383` (pushed). 280 tests green.

- Owner supplied a BotFather token and chose to reuse the existing @agent3assistant_bot.
  Checked before accepting: no webhook, no other poller, token absent from server configs.
  Token and owner tg id (190933209) stored on VPS at `~/.adminbot.env` (600), not in git.
- **Found: `api.telegram.org` does not resolve from this VPS.** The worker bot has long
  used direct IPs (`TELEGRAM_API_IPS`). Ported the trick into `adminbot/tg/session.py`:
  resolves the Telegram host to a probed address, remembers it, other hosts unaffected.
  Verified from the VPS — getMe returns the bot via 149.154.167.220.
- `deploy/install.sh` written and copied to the VPS: system user, code, venv, DB role with
  SELECT-only on `public` plus own schema, three migrations, a check that writes to the
  bot's tables are refused, `.env` (600) with a self-generated DB password, systemd unit.
  Idempotent. NOT executed — needs the owner's sudo password.
- Next session: run the one install command, then `/status` in Telegram, then the staged
  switch-on (rehearsal → `/backlog` preview → «Поехали» → live).
- Deploy SHA: not applicable (service not installed yet).

---

### 2026-08-26 - Deployed to production; amo_sync live; carpets (stage 3) built and live

status: completed
actor: claude
scope: raketa-admin-bot, branch `feature/amo-sync-v1` at `15e3a7a` (pushed). 335 tests green.

- **Deployed**: own system user, DB role with SELECT-only on `public`, migrations 001-004,
  systemd unit. `sudo raketa-admin-bot-update` with NOPASSWD handles updates and switches
  (--enable/--live/--carpets-on/--carpets-live/--backlog-from/--report).
- **amo_sync live**: backlog widened to 2026-08-14 on the owner's call; orders 564-588
  processed, owner verified them in CRM. Question cards, answers, evening summary work.
- **Carpets (stage 3) built in one session**: mail reader (IMAP, read-only), report parser
  (51 orders across 5 real files), matcher, engine, own storage, watcher, Telegram cards.
  Five partner orders processed live; `scripts/verify_carpets.py` reports 0 discrepancies.
- Bugs caught in production, not by tests: (1) reading a letter marked it read, so the
  rehearsal ate both reports and the live run did nothing — now BODY.PEEK; (2) a Telegram
  outage killed the whole process, including CRM work — now retried; (3) rehearsal wrote
  checklist marks to the DB.
- Owner's rules learned: salesbot can lag ~20 min (wait raised to 40); a two-service lead
  is split by the salesbot into two deals, so in the carpet pipeline service and specialist
  must be overwritten; owner messages must show full phone + order date.
- Deploy SHA: `15e3a7a`.

---

### 2026-08-28 - Checker spam fixed: one message per finished calendar record

status: completed; deployed and verified
actor: claude
scope: raketa-admin-bot, branch `feature/amo-sync-v1` at `7f55e3a` (pushed). 461 tests green.

- Owner reported three messages per calendar record. **Two causes, both in the watcher**:
  the report went out in state `waiting_salesbot` too, and that state is re-checked every
  minute; and a record arriving with changes also sat in the pending list, so the engine
  processed it **twice per pass** — extra amoCRM calls, not just extra messages.
- The second cause was found by the reproducing test, not by reading code: the test failed
  with two messages where one was expected, and that extra one led to the double handling.
- Fix: report only when the record is finished (`done`), once, with the deal link; mark
  `done_msg_id` (migration 006) only after Telegram accepted the message — otherwise a
  connection drop would count as "reported". Edit notices and question cards unchanged
  (owner's decision 2026-08-28).
- **Deploy trap hit and documented**: the first `raketa-admin-bot-update` restarted the
  service on the OLD code. The script rsyncs from `/home/admin/raketa-admin-bot`; there is
  no git on the server. Only sign in the output: the new migration is missing from step 4.
  `docs/deploy.md` §9 rewritten to the real two-step procedure.
- Verified on the VPS: migration applied, service active, no errors, a preview run of a
  finished record read the new column and printed a single deal link.
- Deploy SHA: `7f55e3a`.

---### 2026-08-27 - Stage 2 (Google Calendar) built, launched and hardened on live data

status: completed; live under a week of watching
actor: claude
scope: raketa-admin-bot, branch `feature/amo-sync-v1` at `be0ecc3` (pushed). 457 tests green.

- Built stage 2 end to end in one day (10 tasks): parser, exam, Google client, storage,
  matcher, engine, cards, watcher, switches, `/calendar`, evening block, access docs.
  Incremental sync by syncToken — the only way to see cancellations at all.
- **Exam on all 462 real calendar records**: 0 non-orders taken for orders, phone 100%,
  service 99.4%, district 95.7%. Found what recon missed: shorthands (Дзерж, Автоз,
  Ниже, Бог) and plural furniture. Owner then gave the district canon; Балахнинский
  and Дальнеконстантиновский have no value in amo — reported separately, field left empty.
- **Live since 13:10.** Six deals created/filled the same day, each checked by the owner.
- **Five bugs found by live work, not by tests**: rehearsal stayed silent about confident
  decisions; editing a pre-switch-on record woke it up; the robot took an autodeal already
  bound to another record; address and comment were left as amo prefilled them (owner:
  the calendar wins, they are overwritten); a closed deal on the record's date led to a
  silent duplicate (now a question card).
- Owner's new rules: source of the deal (lead's source stays; empty → «Сарафан»; no lead
  and no contact → «Сарафан»; contact without lead → «Повторный») — calendar only, orders
  from the bot keep their own rule.
- **Checker for the week of watching**: every finished job is reported to the owner at once
  with a deal link; record edits are pulled into the deal and announced.
- Deploy SHA: `be0ecc3`.

---

---

### 2026-09-03 (вечер) - выплата прибыли из кассы клининга

status: в проде, первая реальная выплата прошла в тот же день
actor: claude
scope: tgbot-v1, branch `feature/amo-exchange` at `458bf28` (pushed, prod). 228 tests green, 16 new.

- Владелец, Дима и Оля забирают заработанное сразу. Раньше была админская команда
  со свободным комментарием; теперь операция названа своим именем: кнопка «💸 Выплата»
  у клинера (деньги у неё в руках) и та же команда у админов — кнопок в их меню и так много.
- Половина уже была: `record_dividend`, тип `dividend` вне P&L, кассовый чат. Добавлены
  деление на получателей, две проверки и отмена. Миграция не понадобилась.
- Решения владельца: сверяем по общему остатку (способы оплаты в клининге не делят);
  делим поровну, он проследит, чтобы сумма делилась; отменяет только админ; неснижаемого
  остатка и потолка нет; мебель не трогаем.
- Три решения приняты агентом и записаны в коммит: одна строка в кассе вместо трёх
  (деление всегда поровну — три строки не добавили бы фактов, зато отмена стала бы
  групповой); сумма обязана делиться нацело, иначе касса разойдётся с чатом на копейку;
  остаток перепроверяется внутри транзакции при подтверждении — между вводом и «Провести»
  клинер может провести заказ с другого устройства.
- Порядок проверок важен: сначала «хватает ли», потом «делится ли». Обратный порядок
  ответил бы «не делится на 3» человеку, у которого просто нет таких денег.
- Получатели и комментарий — настройки (`CLEANING_DIVIDEND_RECIPIENTS`, `..._COMMENT`).
- **Сессия шла параллельно с другой в этом же репозитории** (клиентские письма). Одна
  попытка `git stash` едва не задела её работу — всё цело, но так больше не делать.

- **Проверено живыми деньгами в тот же день**: владелец провёл командой реальную выплату,
  ждавшую доработки, — строка `#8`, 9 999 ₽ на троих, касса 13 793 → 3 794 ₽, ровно как обещало
  подтверждение. Кнопкой у клинера сценарий ещё не проходили.

deploy SHA: `458bf28`

---

### 2026-09-04 - «веду сам» держится и помечает сделку в амо

status: закоммичено (`fd6767b`), НЕ развёрнуто
actor: claude (из сессии tgbot-v1, с разрешения владельца)
scope: `feature/amo-sync-v1`, gcal. 740 тестов зелёных, 5 новых.

- Кнопки «✋ Сам разберусь» и «✋ Оставить как есть» писали решение только себе
  в базу. Рабочий бот (tgbot-v1) про него не знал и продолжал писать клиенту.
  Живой случай: клиентка отменила работу, владелец нажал «Оставить как есть»,
  а рабочий бот собирался спросить её про тот же заказ.
- **Найден баг: решение не держалось и внутри этого бота.** Запись оставалась
  в `skipped`, следующий проход принимал это за снятую пометку «⁉️»
  (решение 9) и возвращал её в работу. Проверено на живом коде до правки:
  робот переписывал поля сделки поверх правок владельца и закрывал запись
  как проведённую.
- `HANDS_OFF_REASONS` — общее правило «робот не лезет»: запись до включения
  плюс оба решения владельца. Пробуждение по снятой «⁉️» осталось.
- Новый шаг `marking`: галочку ставит движок, не Telegram-обработчик —
  решение переживает перезапуск, как и закрытие сделки.
- Помечаем только то, что робот про заказ уже знает: сделку реализации, а
  если её нет — первичный лид. Кандидатов из списка не метим никогда.
- Поле амо `FIELD_OWNER_HANDLES = 1461165` (галочку завёл владелец 04.09).
- Карточки выполненных заказов и ковровые не тронуты — по решению владельца.

---


### 2026-09-04 — чужие заказы, ожидание сделки и галочка «веду сам»

status: выкачено в прод, оба бота
actor: claude
scope: tgbot-v1 `feature/amo-exchange` → `aa4a3dc` (прод, 13:38 MSK), 240 тестов.
       raketa-admin-bot `feature/amo-sync-v1` → `fd6767b` (прод, 13:43 MSK), 740 тестов.

- Разбор трёх хвостов от 03.09. Прокси за сутки: 2 сетевые ошибки против 32 без
  него, 0 перезапусков, 30 из 30 живых запросов, медиана 0,45 с. Сторож молчунов
  сработал живьём 03.09 в 20:38 — числился непроверенным.
- **Найдена дыра**: робот включается по «Передано в работу», а это развилка на
  шесть воронок. Проверка воронки стояла только на одной из двух дорог к дочерней
  сделке; ковровый заказ прошёл по второй и получил письма как уборка. 1 из 15
  заказов за неделю. Теперь берём только «Воронку для реализации».
- **Дочерней сделки нет — ждём час, а не пишем вслепую** (новый проход добора).
  Раньше письмо уходило по данным лида и оставалось без номера сделки.
- **Галочка «заказ ведёт владелец»** — поле амо 1461165, метка живёт в CRM:
  это единственный общий язык двух ботов. Слушается в трёх местах, важнее всего
  перед записью в CRM. Галочка на лиде до перевода отключает робота целиком.
- **В админ-боте найден и исправлен баг**: кнопка «Сам разберусь» не держалась —
  проход принимал `skipped` за снятую «⁉️» и возвращал запись в работу,
  переписывая поля сделки поверх правок владельца. Проверено прогоном движка.
- Решения владельца: галочка — поле, а не тег; пропуск чужой воронки — строкой
  в суточной сводке без тревоги; переделки по гарантии не трогаем (2 в месяц).
- Руками поправлены 4 записи, испорченные логикой до 03.09: отменены вопросы
  по удалённой сделке, по коврам и по клиентке, которую владелец ведёт сам;
  клиентке 5772 возвращён слух — ложное «Спасибо» стёрто.

deploy SHA: `aa4a3dc` (tgbot-v1), `fd6767b` (raketa-admin-bot)

### 2026-09-10 (вечер) — уборки идут в амо своим потоком

status: развёрнуто 19:28 МСК, выключатели уборок ВЫКЛЮЧЕНЫ — включает владелец
actor: claude (Opus-исполнитель по ТЗ; координация из сессии tgbot-v1)
scope: `feature/amo-sync-v1` → `dc49552`, 6 коммитов, 811 тестов зелёных (было 783).

- ТЗ: `docs/plans/2026-09-10-cleaning-orders-sync.md`. Уборки не проводились
  вовсе: клининг-контур пишет в `public.cleaning_orders`, робот читал `orders`.
- Второй поток поверх того же движка: свой источник, своя таблица связок
  (миграция 010, `adminbot.cleaning_links`), свои выключатели
  `CLEANING_SYNC_ENABLED` / `CLEANING_SYNC_DRY_RUN` (оба выключены) и
  `CLEANING_BACKLOG_FROM` (пусто — хвост со дня включения).
- Решения владельца в коде: «Специалист» всегда Ольга (952251), «Услуга»
  всегда «Уборка» (772345), «Подарочный сертификат» → «Сертификат + деньги»
  (884545), «Расчётный» → безнал и юрлицо. «Карта» → «Перевод на карту»
  (235851) — допущение, владелец не возражал.
- Занятость сделок считается по обеим таблицам связок: уборка и химчистка
  одному клиенту в один день — две разные сделки. Кнопки уборок получили свою
  приставку (`amoclean:`), иначе ответ по «Уборке №5» ушёл бы в «Заказ №5».
- Задачу «Получить ОС» у уборок не закрываем: оценки клиента в клининге нет.
- Значения полей сверены вживую перед выкаткой: оплаты (235849/235851/235857/
  884545), Ольга (952251), «Уборка» (772345) — все совпали с кодом.
- Исполнитель остановился на лимите, не дописав журнал; тесты, сверку полей,
  выкатку и запись доделала координирующая сессия.

deploy SHA: `dc49552` (хеши пяти ключевых файлов сверены, миграция 010
применена, служба active, ошибок нет).
---
### 2026-09-10 — письмо клиенту «заказ отменён»

status: выкачено в прод 18:44 МСК по слову владельца, боевой режим
actor: claude (Opus по ТЗ из raketa-admin-bot)
scope: tgbot-v1 `feature/amo-exchange` → `11e276a`, 307 тестов (+19).

- ТЗ: `raketa-admin-bot/docs/plans/2026-09-10-cancel-letter-to-client.md`.
  Клиент не узнавал об отмене, хотя «Ваш заказ принят» ему уже приходило.
- Сигнал идёт через карточку амо, а не через базу: админ-бот в таблицы
  рабочего бота не пишет. Проход разговора с клиентом раз в 10 минут читает
  карточки свежих ожиданий; закрыта (143) с причиной «Пропала потребность»
  (поле 19215, enum 8299) — письмо в общую очередь, каналы и лимиты общие.
- Одно письмо на сделку держит колонка `order_confirmations.cancel_notified_at`;
  ещё не ушедший вопрос «подтверждаете заказ?» отменяется тем же ходом.
- Выключатель общий с «Ваш заказ принят», отдельного нет — решение владельца.
  «Отмен сообщено» встало в суточную сводку.
- Причина сверяется по номеру варианта (`field_enum_ids`), не по подписи:
  подпись в амо переименовывается одним кликом.
- Решения владельца: письмо по любой сделке с этой причиной, хоть роботом
  закрытой, хоть руками; только тем, с кем робот про заказ уже говорил; текст
  дословный. Порог «сутки после дня работы» — допущение агента.
- Выборку и отмену письма проверил на живом Postgres, не только тестами.
- Хвост: владельцу закрыть тестовую сделку с этой причиной и дождаться прохода.
  Живого случая отмены после выкатки ещё не было.

deploy SHA: `11e276a` (старт чистый, колонка и индекс на месте,
`CLIENT_MESSAGING_ENABLED=1`, `DRY_RUN=0`).
---

### 2026-09-11 — бриф: монорепо и адреса; первая живая проверка адресной механики

status: бриф написан и закоммичен локально (`a9a4fab`); в origin не отправлен по решению владельца
actor: claude (Opus; Fable недоступна по лимиту, владелец разрешил вести сессию здесь)
scope: `feature/amo-exchange` → `a9a4fab`, боевой код не трогали, прод остаётся на `11e276a`.

- Владелец решил слить `tgbot-v1` и `raketa-admin-bot` в один репозиторий:
  сливаются папки и истории, две службы на сервере остаются. План слияния
  общего кода (словарь полей амо, клиент API) — отдельным шагом.
- Порядок работ: адреса → монорепозиторий → уведомления рабочего бота админам.
- Первая живая проверка адресной механики (выкачена 06.09, до сегодня никто
  не смотрел): 9 заказов с 07.09, у 7 адрес взят из сделки амо, у 2 — откат
  на карточку клиента.
- Разбор двух отказов: рабочий бот ни при чём. Сделки четыре дня стояли
  «Новым лидом»; робот не отреагировал ни на появление записи в календаре,
  ни на перенос даты. Адрес попал в амо из карточки клиента уже в момент
  проведения. Разбор за владельцем в понедельник, механизм выбора сделки
  (`pick_open_deal`) не трогаем.
- Решения по адресам: вопрос про адрес убрать из диалога мастера; адрес
  в карточке клиента обновлять после заказа; админ-бот читает адрес из заказа;
  в уборках сделать ту же механику (там адрес вводится руками, 4 уборки за всю историю).
- Бриф: `docs/plans/2026-09-11-brief-monorepo-and-addresses.md` — состав работ,
  долги и раздел спорных моментов сессии (три ошибочных утверждения агента).

deploy SHA: не деплоили.
---
### 2026-09-14 — монорепозиторий raketaclean: два бота в одном репозитории, переименование, деплой

status: сделано и выкачено; контекст остаётся в agent1 до отдельной сессии по решению владельца
actor: claude (Fable — координация, ТЗ, деплой; implementer Opus ×2; reviewer Opus)
scope: `tgbot-v1` → `raketaclean`, `main` `b4c463a` (pushed), история 662 записей; прод рабочего бота `b4c463a`.

- Порядок работ изменён владельцем: монорепозиторий первым, адреса после.
- ТЗ `docs/plans/2026-09-14-monorepo.md`. Решения: рабочий бот в корне, админ-бот в подпапке
  `raketa-admin-bot/` со всей историей (git filter-repo); `main` подтянут до боевых веток
  в обоих репозиториях; разведка в `docs/recon/`; один контекст на оба бота.
- Чистка: семь `bot.py.bak.*` убраны, три полных телефона в разведке замаскированы, `/recon/` в `.gitignore`.
- Правила: корневой `CLAUDE.md` на две службы, у админ-бота своя специфика; единые `.claude/settings.json`;
  корневой `pytest.ini`; `docs/deploy.md` рабочего бота (runbook раньше не было).
- Ревью задач 1–5: соответствует; из 4 замечаний два закрыты (путь в `history_exam.py`, пять правил
  админ-бота возвращены), остальное в «на потом» состояния.
- GitHub: репозиторий переименован в `raketaclean`, старый `raketa-admin-bot` в архиве (только чтение),
  локальная папка в `~/Projects/archive/raketa-admin-bot_ARCHIVE_2026-09-14`.
- Деплой рабочего бота 15:24 МСК: сервер с `feature/amo-exchange` на `main`, боевой код побайтно тот же
  (diff до сервера пуст), служба active, 0 ошибок. Админ-бот не деплоился; сухой прогон rsync из подпапки:
  по содержимому 6 файлов (правила, README, deploy.md, два скрипта, тест), кода нет.
- Контекст: в agent1 слиты состояния и журналы обоих проектов (`project_ai_context/raketaclean/`,
  коммит agent1 `2b6cd27`), реестр и карта инфраструктуры переписаны, память скопирована (23 заметки).
- Параллельно шла сессия agent1 по эталону: её коммиты подхватывали наши переименования в общем индексе, без потерь.
- Локальная база `adminbot_test` создана для тестов админ-бота (её не было).

deploy SHA: `b4c463a` (рабочий бот, `main`). Не запушено: коммит с отметками «Выполнено» в ТЗ.

### 2026-09-15 — ковры: проверка, инцидент с двухлетним отчётом, разбор 66 висячих сделок, резервные DNS

status: сделано; ковры включены обратно (боевой режим 14:06), контекст обновлён
actor: claude (Fable, в текущей сессии; субагентов не было)
scope: `main` `709a7e9` плюс коммит контекста; прод рабочего бота `b4c463a`, админ-бота `dc49552` (код ботов не менялся)

- Проверка ковров: 4 отчёта, 27 строк, сверка с амо без расхождений; отчёта за 31.08–06.09 нет
  ни в одной папке; в «Ковры Кристал» 78 висячих сделок, 76 старше запуска робота.
- DNS провайдера молчал 11:44–12:28, оба бота теряли амо и календарь. Скрипт
  `scripts/server_dns_backup.sh` (Яндекс + Cloudflare через netplan) и раздел в `docs/deploy.md`;
  владелец применил, текущий сервер 77.88.8.8, провайдерские остались.
- ИНЦИДЕНТ: партнёр прислал отчёт за 2 года (536 строк) на ящик робота, фильтр переложил его
  в `robot_amo`, робот взял в 13:29; остановлен в 13:33 после 41 строки (12 закрыл, 13 привязал,
  16 завёл с нуля → 30 новых сделок). API амо удалять не умеет: владелец удалил 30 сделок руками,
  память робота вычищена скриптом `/home/admin/forget_carpet_rows.sh`, служба поднята без ковров
  в 13:36, ковры включены 14:06 после трёх проверок.
- Разбор 66 висячих: сверка выгрузки амо с файлом партнёра (`~/Downloads/amocrm_66_vs_partner.xlsx`),
  репетиция по всему файлу (52 use_carpet, 7 ask_owner, 315 create_new на 954 т.р.), боевые прогоны
  с белым списком сделок (обёртка `/home/admin/live_rows.py`): 39 доставлено, 22 закрыто как
  не реализовано (14 без пары старше 2 мес., 7 отказов партнёра, 1 дубль), 5 моложе 2 мес. открыты.
- Решения владельца: 315 заказов партнёра без сделки в амо не заводить; сбор обрывов связи отложен
  к задаче «уведомления и логи»; 20 «новых лидов» слушает сам.
- Память: заметка `carpets-mailbox-is-live-input`. `.gitignore`: `/*.xlsx` (файл партнёра в корне).

deploy SHA: не деплоили. Не запушено: `a01095d`, `709a7e9` и коммит контекста.
---

### 2026-09-16 — защита ковров от «не того файла»: ТЗ, исполнение, ревью, деплой, память по файлу партнёра

status: сделано и выкачено; сессия 15–16.09 закрыта
actor: claude (Fable — координация и ТЗ; implementer Opus ×2; reviewer Opus)
scope: `main` `7720306` (запушен владельцем), 32 коммита за сессию; прод админ-бота `7720306` с 16.09 11:47 МСК.

- Устройство защиты обсуждено с владельцем: партнёр шлёт недельный, месячный и файл отказов,
  повторы отсекает номер заказа; порог 100 наших строк, без проверки дат, без кнопок в Telegram.
- ТЗ `raketa-admin-bot/docs/plans/2026-09-15-carpets-letter-guard.md`, 7 задач: IMAP UID вместо
  порядковых номеров писем; миграция 011 (отложенные письма); проверка письма целиком до первой
  строки, `CARPETS_MAX_ROWS=100`, одно сообщение владельцу; ключи `--carpets-held`,
  `--carpets-release=UID`, `--carpets-remember=ФАЙЛ [--carpets-remember-live]`;
  `run_carpets.py --file --allow` (боевой прогон из файла только с белым списком сделок).
- Отступление исполнителя принято: четвёртое состояние письма `released`, порог к нему не применяется.
- Ревью: 7 замечаний, 6 в работу (запись в память одной операцией и починка недоделанных строк;
  имя файла партнёра в `source_file`; `--report` прячет запомненные; ключи с пустым аргументом
  не ведут к деплою; две правки доки), 9 «на потом» в `…-review.md`. Правки свежим исполнителем
  плюс одна четырёхстрочная координатором. Тесты 811 → 831 зелёных.
- Деплой владельца 15.09 не доехал (update без rsync, старый источник). 16.09 деплой по runbook,
  миграция 011, 12 ключевых файлов равны `main` по хешам, ковры в боевом режиме.
- `--carpets-remember` по двухлетнему файлу: 536 строк, 41 знал, 495 запомнил, повтор 0 новых;
  файл с сервера удалён. Ветка `feature/carpets-letter-guard` удалена после слияния.
- Правило моделей изменено владельцем 16.09 в глобальном `CLAUDE.md`: главная сессия Opus,
  субагенты Sonnet, Fable по прямой просьбе. `session-report` не запускался (разрешения не было).

deploy SHA: `7720306` (админ-бот). Не запушено: коммит контекста и отметка приёмки в ТЗ.

---

---

### 2026-09-16 (вечер) — уборки в бою, пакет из пяти правок, деплой обоих ботов

status: сделано и выкачено
actor: claude (Opus — координация, разведка, отладка, деплой; implementer Sonnet ×3; reviewer Sonnet)
scope: `main` `d65b793` (запушен), ветка `feature/fixes-2026-09-16` слита; прод рабочего бота `d65b793`, админ-бот равен `main` по хешам.

- **Уборки включены**: сверка списков амо → репетиция 13:09 → бой 13:44. Живая проверка владельца прошла: робот за 4 минуты завёл сделку, заполнил по решениям 10.09 (услуга «Уборка», специалист Ольга, бригадир в примечание, «Получить ОС» оставил владельцу) и довёл до успеха. Тестовая уборка потом откачена штатной операцией бота (касса и бонусы вернулись).
- Выяснено по ходу проверки: репетиция не может пройти путь «новый клиент» до конца — он ждёт автосделку сейлзбота, которой в репетиции нет.
- **Письмо «заказ отменён»**: за всё время не уходило ни разу. Причина не в поломке — единственный подходящий случай был 11.09, когда новый код ещё не работал на сервере, и он же не прошёл бы по порогу «сутки от времени работы».
- **Уведомление клиенту об уборке** ушло штатно, в WhatsApp; владелец его не увидел, потому что сменил номер. Переотправка вторым каналом работает, но идёт по одному сообщению с паузой 1–60 минут — решение владельца оставить как есть.
- **Пакет правок** (ТЗ `docs/plans/2026-09-16-package-fixes.md`, 5 задач): кнопки бригадира «💸 Выплата» и «🔍 Клиент» (упирались в заглушку «команда не распознана»); время работы по Москве вместо UTC в трёх местах (касалось и химчистки с августа); пометка «🎭 РЕПЕТИЦИЯ» в сообщениях робота; шесть пунктов косметики ковров; три команды клининга в синее меню админа.
- Ревью пакета: одно замечание (пометка репетиции не доходила до ручной команды `/carpets`), исправлено свежим исполнителем. Тесты: рабочий бот 307 → 313, админ-бот 831 → 843.
- **Найдено и отложено в парк**: заглушка `unknown` в `bot.py` перехватывает текст раньше модуля клининга, поэтому каждая новая кнопка бригадира будет ломаться, пока её не впишут в мост.
- Решения владельца: время чинить везде; из десяти пунктов косметики ковров взять шесть, четыре вычеркнуть; досылку сообщений менеджеру при обрыве не делать; команды клининга — в синее меню, без кнопок.
- Память агента: архив стенограмм двух старых проектов перенесён в каталог `raketaclean`, старые папки владелец удаляет сам.

deploy SHA: `d65b793` (оба бота). Незапушенного нет.
