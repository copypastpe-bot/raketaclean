# Карта функций `cleaning/handlers.py`

1526 строк. Роутер aiogram со всеми диалогами клининга: проведение уборки,
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
| 63 | `_bonus_expire_label` | дата сгорания бонусов человеку, пусто → «—» |
| 71 | `_period_bounds` | `day\|month\|year` → границы периода в UTC и подпись |
| 137 | `_pay_method_kb` | клавиатура способов оплаты; `allow_wire=False` убирает безнал |
| 147 | `_comment_kb` | клавиатура шага комментария («Без комментария») |
| 157 | `_expense_category_kb` | категории расхода плюс «Готово» |
| 166 | `_yes_no_kb` | да/нет |
| 176 | `_confirm_kb` | «Провести» / «Отмена» |
| 186 | `cleaning_main_kb` | главное меню клининга (экспортируется в `bot.py`) |
| 207 | `_is_valid_phone` | 10-11 цифр в строке |
| 212 | `_money_str` | сумма без лишних нулей для сообщений |

## Доступ

| Строка | Функция | Назначение |
|---|---|---|
| 197 | `_is_foreman` | может ли этот Telegram-пользователь проводить уборки |
| 202 | `_has_permission` | проверка именного права (`cleaning_*`) |

## Очереди на сторону: уведомления и отчёт

| Строка | Функция | Назначение |
|---|---|---|
| 219 | `_enqueue_cleaning_completed_notifications` | ставит клиенту письма по правилам уведомлений и отмечает у уборки «попросили оценить» (`rating_requested_at`, прошлый ответ стирается) |
| 572 | `_enqueue_cleaning_order_report` | кладёт уборку в `pending_order_reports` (`kind='cleaning'`); отчёт уйдёт из `bot.py`, когда админ-бот заведёт связку с адресом |

## Проведение уборки (`CleaningOrderFSM`)

Порядок шагов: телефон → имя → комментарий → сумма → бонусы → способ оплаты →
сумма оплаты → расходы → подтверждение → проведение.

| Строка | Функция | Назначение |
|---|---|---|
| 285 | `start_cleaning_order` | вход: команда `/cleaning_order` или кнопка «🧹 Провести уборку» |
| 313 | `cancel` | «Отмена» на любом шаге |
| 319 | `got_phone` | телефон, поиск клиента, показ бонусов |
| 355 | `got_name` | имя клиента |
| 366 | `got_comment` | необязательный комментарий (сюда бригадир пишет адрес, если хочет) |
| 376 | `got_amount` | сумма чека |
| 394 | `got_bonus_spend` | сколько бонусов списать |
| 418 | `got_pay_method` | способ оплаты |
| 476 | `got_pay_amount` | сумма по выбранному способу (бывает несколько оплат) |
| 513 | `got_expense_category` | категория расхода или «Готово» |
| 527 | `got_expense_amount` | сумма расхода |
| 544 | `_show_confirm` | сводка перед проведением |
| 621 | `do_provesti` | **главный обработчик**: одна транзакция — клиент (639-654), приход (706, `cash_holder`: «Наличные»/«Карта» → `olya`, «Расчётный» → `dima`), расходы (721, `olya`), бонусы, уведомления клиенту (768), отчёт в очередь (799) |

## Баланс и поиск клиента

| Строка | Функция | Назначение |
|---|---|---|
| 829 | `cleaning_balance_cmd` | баланс кассы клининга (право `cleaning_view_balance`) |
| 843 | `cleaning_client_lookup_start` | кнопка «🔍 Клиент» (право `cleaning_view_clients`) |
| 854 | `cleaning_client_lookup_phone` | карточка клиента по телефону |

## Расход бригадира (`CleaningForemanExpenseFSM`)

| Строка | Функция | Назначение |
|---|---|---|
| 886 | `foreman_expense_start` | «➖ Добавить расход» (право `cleaning_record_expense`) |
| 897 | `foreman_expense_amount` | сумма |
| 908 | `foreman_expense_category` | категория |
| 926 | `foreman_expense_comment` | комментарий |
| 944 | `foreman_expense_confirm` | проведение и сообщение в кассу; строка из денег Оли (`cash_holder='olya'`) |

## Выплата прибыли (`CleaningDividendFSM`)

| Строка | Функция | Назначение |
|---|---|---|
| 982 | `_start_dividend` | общее начало: показать баланс, спросить сумму |
| 995 | `start_cleaning_dividend` | команда `/cleaning_dividend` (право `cleaning_manage_cash`) |
| 1004 | `start_cleaning_payout_button` | текст «💸 Выплата» (право `cleaning_pay_dividend`; с 05.10 у клинера ни кнопки, ни права — только админы) |
| 1013 | `div_amount` | сумма выплаты и расчёт долей |
| 1055 | `div_provesti` | проведение выплаты (`cash_holder='dima'`, выбор «Оля/Дима» — ТЗ 2026-10-05, задача 3) |
| 1105 | `start_dividend_cancel` | `/cleaning_dividend_cancel N` |
| 1139 | `dividend_cancel_confirmed` | отмена выплаты |

## Касса вручную

| Строка | Функция | Назначение |
|---|---|---|
| 1171 | `start_cash_add` | `/cleaning_cash_add` — приход (право `cleaning_manage_cash`) |
| 1191 | `cash_add_method` | способ |
| 1201 | `cash_add_amount` | сумма |
| 1212 | `cash_add_comment` | комментарий |
| 1227 | `cash_add_provesti` | проведение прихода (`cash_holder='dima'` до задачи 3) |
| 1263 | `start_cash_expense` | `/cleaning_cash_expense` — расход |
| 1277 | `cash_exp_category` | категория |
| 1290 | `cash_exp_amount` | сумма |
| 1301 | `cash_exp_comment` | комментарий |
| 1316 | `cash_exp_provesti` | проведение расхода (`cash_holder='dima'` до задачи 3) |
| 1352 | `start_cash_withdrawal` | `/cleaning_cash_withdrawal` — изъятие |
| 1366 | `cash_wd_amount` | сумма |
| 1377 | `cash_wd_comment` | комментарий |
| 1392 | `cash_wd_provesti` | проведение изъятия (`cash_holder='dima'` до задачи 3) |

## Отмена уборки и отчёты

| Строка | Функция | Назначение |
|---|---|---|
| 1426 | `start_cancel_order` | `/cleaning_cancel_order N` — спросить подтверждение |
| 1450 | `cancel_order_confirmed` | откат уборки: `deleted_at` у заказа, мягкое удаление кассовых строк, возврат бонусов (`admin_ops.cancel_order`) |
| 1488 | `cleaning_cash_report` | `/cleaning_cash day\|month\|year` |
| 1512 | `cleaning_orders_list` | `/cleaning_orders day\|month` |
