# Карта функций `cleaning/handlers.py`

1545 строк. Роутер aiogram со всеми диалогами клининга: проведение уборки,
касса, выплата прибыли, отмена. Подключается в `bot.py` через
`dp.include_router(cleaning_router)`.

Как читать: почти всё здесь — шаги пошаговых диалогов (FSM). Один диалог —
это цепочка «старт → вопрос → вопрос → подтверждение → проведение»; деньги
пишутся только на последнем шаге, внутри одной транзакции.

Осторожно: заглушка «команда не распознана» в `bot.py` ловит текст раньше
этого роутера, поэтому каждая новая текстовая кнопка бригадира не работает,
пока её не впишут в мост (известное ограничение, `AGENT_STATE.md`).

## Клавиатуры и мелкие помощники

| Строка | Функция | Назначение |
|---|---|---|
| 64 | `_bonus_expire_label` | дата сгорания бонусов человеку, пусто → «—» |
| 72 | `_period_bounds` | `day\|month\|year` → границы периода в UTC и подпись |
| 138 | `_pay_method_kb` | клавиатура способов оплаты; `allow_wire=False` убирает безнал |
| 148 | `_comment_kb` | клавиатура шага комментария («Без комментария») |
| 158 | `_expense_category_kb` | категории расхода плюс «Готово» |
| 167 | `_yes_no_kb` | да/нет |
| 177 | `_confirm_kb` | «Провести» / «Отмена» |
| 187 | `cleaning_main_kb` | главное меню клининга (экспортируется в `bot.py`) |
| 208 | `_is_valid_phone` | 10-11 цифр в строке |
| 213 | `_money_str` | сумма без лишних нулей для сообщений |

## Доступ

| Строка | Функция | Назначение |
|---|---|---|
| 198 | `_is_foreman` | может ли этот Telegram-пользователь проводить уборки |
| 203 | `_has_permission` | проверка именного права (`cleaning_*`) |

## Очереди на сторону: уведомления и отчёт

| Строка | Функция | Назначение |
|---|---|---|
| 220 | `_enqueue_cleaning_completed_notifications` | ставит клиенту письма по правилам уведомлений и отмечает у уборки «попросили оценить» (`rating_requested_at`, прошлый ответ стирается) |
| 573 | `_enqueue_cleaning_order_report` | кладёт уборку в `pending_order_reports` (`kind='cleaning'`); отчёт уйдёт из `bot.py`, когда админ-бот заведёт связку с адресом; в payload и остаток денег Оли `olya_balance` (None — уборка их не задела) |

## Проведение уборки (`CleaningOrderFSM`)

Порядок шагов: телефон → имя → комментарий → сумма → бонусы → способ оплаты →
сумма оплаты → расходы → подтверждение → проведение.

| Строка | Функция | Назначение |
|---|---|---|
| 286 | `start_cleaning_order` | вход: команда `/cleaning_order` или кнопка «🧹 Провести уборку» |
| 314 | `cancel` | «Отмена» на любом шаге |
| 320 | `got_phone` | телефон, поиск клиента, показ бонусов |
| 356 | `got_name` | имя клиента |
| 367 | `got_comment` | необязательный комментарий (сюда бригадир пишет адрес, если хочет) |
| 377 | `got_amount` | сумма чека |
| 395 | `got_bonus_spend` | сколько бонусов списать |
| 419 | `got_pay_method` | способ оплаты |
| 477 | `got_pay_amount` | сумма по выбранному способу (бывает несколько оплат) |
| 514 | `got_expense_category` | категория расхода или «Готово» |
| 528 | `got_expense_amount` | сумма расхода |
| 545 | `_show_confirm` | сводка перед проведением |
| 627 | `_income_cash_holder` | чьи деньги приход по уборке: «Наличные»/«Карта» → `olya`, остальное («Расчётный») → `dima` |
| 636 | `do_provesti` | **главный обработчик**: одна транзакция — клиент (654-669), приход (722, `cash_holder` через `_income_cash_holder`), расходы (734, `olya`), бонусы, уведомления клиенту (782), остаток Оли (798, только если уборка задела её деньги), отчёт в очередь (815) |

## Баланс и поиск клиента

| Строка | Функция | Назначение |
|---|---|---|
| 846 | `cleaning_balance_cmd` | баланс кассы клининга (право `cleaning_view_balance`) |
| 860 | `cleaning_client_lookup_start` | кнопка «🔍 Клиент» (право `cleaning_view_clients`) |
| 871 | `cleaning_client_lookup_phone` | карточка клиента по телефону |

## Расход бригадира (`CleaningForemanExpenseFSM`)

| Строка | Функция | Назначение |
|---|---|---|
| 903 | `foreman_expense_start` | «➖ Добавить расход» (право `cleaning_record_expense`) |
| 914 | `foreman_expense_amount` | сумма |
| 925 | `foreman_expense_category` | категория |
| 943 | `foreman_expense_comment` | комментарий |
| 961 | `foreman_expense_confirm` | проведение и сообщение в кассу; строка из денег Оли (`cash_holder='olya'`), в сообщении строка «Деньги Оли: N₽» |

## Выплата прибыли (`CleaningDividendFSM`)

| Строка | Функция | Назначение |
|---|---|---|
| 1001 | `_start_dividend` | общее начало: показать баланс, спросить сумму |
| 1014 | `start_cleaning_dividend` | команда `/cleaning_dividend` (право `cleaning_manage_cash`) |
| 1023 | `start_cleaning_payout_button` | текст «💸 Выплата» (право `cleaning_pay_dividend`; с 05.10 у клинера ни кнопки, ни права — только админы) |
| 1032 | `div_amount` | сумма выплаты и расчёт долей |
| 1074 | `div_provesti` | проведение выплаты (`cash_holder='dima'`, выбор «Оля/Дима» — ТЗ 2026-10-05, задача 3) |
| 1124 | `start_dividend_cancel` | `/cleaning_dividend_cancel N` |
| 1158 | `dividend_cancel_confirmed` | отмена выплаты |

## Касса вручную

| Строка | Функция | Назначение |
|---|---|---|
| 1190 | `start_cash_add` | `/cleaning_cash_add` — приход (право `cleaning_manage_cash`) |
| 1210 | `cash_add_method` | способ |
| 1220 | `cash_add_amount` | сумма |
| 1231 | `cash_add_comment` | комментарий |
| 1246 | `cash_add_provesti` | проведение прихода (`cash_holder='dima'` до задачи 3) |
| 1282 | `start_cash_expense` | `/cleaning_cash_expense` — расход |
| 1296 | `cash_exp_category` | категория |
| 1309 | `cash_exp_amount` | сумма |
| 1320 | `cash_exp_comment` | комментарий |
| 1335 | `cash_exp_provesti` | проведение расхода (`cash_holder='dima'` до задачи 3) |
| 1371 | `start_cash_withdrawal` | `/cleaning_cash_withdrawal` — изъятие |
| 1385 | `cash_wd_amount` | сумма |
| 1396 | `cash_wd_comment` | комментарий |
| 1411 | `cash_wd_provesti` | проведение изъятия (`cash_holder='dima'` до задачи 3) |

## Отмена уборки и отчёты

| Строка | Функция | Назначение |
|---|---|---|
| 1445 | `start_cancel_order` | `/cleaning_cancel_order N` — спросить подтверждение |
| 1469 | `cancel_order_confirmed` | откат уборки: `deleted_at` у заказа, мягкое удаление кассовых строк, возврат бонусов (`admin_ops.cancel_order`) |
| 1507 | `cleaning_cash_report` | `/cleaning_cash day\|month\|year` |
| 1531 | `cleaning_orders_list` | `/cleaning_orders day\|month` |
