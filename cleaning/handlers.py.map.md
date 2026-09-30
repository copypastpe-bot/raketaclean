# Карта функций `cleaning/handlers.py`

1500 строк. Роутер aiogram со всеми диалогами клининга: проведение уборки,
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
| 60 | `_bonus_expire_label` | дата сгорания бонусов человеку, пусто → «—» |
| 68 | `_period_bounds` | `day\|month\|year` → границы периода в UTC и подпись |
| 134 | `_pay_method_kb` | клавиатура способов оплаты; `allow_wire=False` убирает безнал |
| 144 | `_comment_kb` | клавиатура шага комментария («Без комментария») |
| 154 | `_expense_category_kb` | категории расхода плюс «Готово» |
| 163 | `_yes_no_kb` | да/нет |
| 173 | `_confirm_kb` | «Провести» / «Отмена» |
| 183 | `cleaning_main_kb` | главное меню клининга (экспортируется в `bot.py`) |
| 205 | `_is_valid_phone` | 10-11 цифр в строке |
| 210 | `_money_str` | сумма без лишних нулей для сообщений |

## Доступ

| Строка | Функция | Назначение |
|---|---|---|
| 195 | `_is_foreman` | может ли этот Telegram-пользователь проводить уборки |
| 200 | `_has_permission` | проверка именного права (`cleaning_*`) |

## Очереди на сторону: уведомления и отчёт

| Строка | Функция | Назначение |
|---|---|---|
| 217 | `_enqueue_cleaning_completed_notifications` | ставит клиенту письма по правилам уведомлений и отмечает у уборки «попросили оценить» (`rating_requested_at`, прошлый ответ стирается) |
| 570 | `_enqueue_cleaning_order_report` | кладёт уборку в `pending_order_reports` (`kind='cleaning'`); отчёт уйдёт из `bot.py`, когда админ-бот заведёт связку с адресом |

## Проведение уборки (`CleaningOrderFSM`)

Порядок шагов: телефон → имя → комментарий → сумма → бонусы → способ оплаты →
сумма оплаты → расходы → подтверждение → проведение.

| Строка | Функция | Назначение |
|---|---|---|
| 283 | `start_cleaning_order` | вход: команда `/cleaning_order` или кнопка «🧹 Провести уборку» |
| 311 | `cancel` | «Отмена» на любом шаге |
| 317 | `got_phone` | телефон, поиск клиента, показ бонусов |
| 353 | `got_name` | имя клиента |
| 364 | `got_comment` | необязательный комментарий (сюда бригадир пишет адрес, если хочет) |
| 374 | `got_amount` | сумма чека |
| 392 | `got_bonus_spend` | сколько бонусов списать |
| 416 | `got_pay_method` | способ оплаты |
| 474 | `got_pay_amount` | сумма по выбранному способу (бывает несколько оплат) |
| 511 | `got_expense_category` | категория расхода или «Готово» |
| 525 | `got_expense_amount` | сумма расхода |
| 542 | `_show_confirm` | сводка перед проведением |
| 619 | `do_provesti` | **главный обработчик**: одна транзакция — клиент (623-633), приход (692), расходы (702), бонусы, уведомления клиенту (746), отчёт в очередь (777) |

## Баланс и поиск клиента

| Строка | Функция | Назначение |
|---|---|---|
| 820 | `cleaning_balance_cmd` | баланс кассы клининга (право `cleaning_view_balance`) |
| 834 | `cleaning_client_lookup_start` | кнопка «🔍 Клиент» (право `cleaning_view_clients`) |
| 845 | `cleaning_client_lookup_phone` | карточка клиента по телефону |

## Расход бригадира (`CleaningForemanExpenseFSM`)

| Строка | Функция | Назначение |
|---|---|---|
| 877 | `foreman_expense_start` | «➖ Добавить расход» (право `cleaning_record_expense`) |
| 888 | `foreman_expense_amount` | сумма |
| 899 | `foreman_expense_category` | категория |
| 917 | `foreman_expense_comment` | комментарий |
| 935 | `foreman_expense_confirm` | проведение и сообщение в кассу |

## Выплата прибыли (`CleaningDividendFSM`)

| Строка | Функция | Назначение |
|---|---|---|
| 969 | `_start_dividend` | общее начало: показать баланс, спросить сумму |
| 982 | `start_cleaning_dividend` | команда `/cleaning_dividend` (право `cleaning_manage_cash`) |
| 991 | `start_cleaning_payout_button` | кнопка «💸 Выплата» (право `cleaning_pay_dividend`) |
| 1000 | `div_amount` | сумма выплаты и расчёт долей |
| 1042 | `div_provesti` | проведение выплаты |
| 1090 | `start_dividend_cancel` | `/cleaning_dividend_cancel N` |
| 1124 | `dividend_cancel_confirmed` | отмена выплаты |

## Касса вручную

| Строка | Функция | Назначение |
|---|---|---|
| 1156 | `start_cash_add` | `/cleaning_cash_add` — приход (право `cleaning_manage_cash`) |
| 1176 | `cash_add_method` | способ |
| 1186 | `cash_add_amount` | сумма |
| 1197 | `cash_add_comment` | комментарий |
| 1212 | `cash_add_provesti` | проведение прихода |
| 1244 | `start_cash_expense` | `/cleaning_cash_expense` — расход |
| 1258 | `cash_exp_category` | категория |
| 1271 | `cash_exp_amount` | сумма |
| 1282 | `cash_exp_comment` | комментарий |
| 1297 | `cash_exp_provesti` | проведение расхода |
| 1329 | `start_cash_withdrawal` | `/cleaning_cash_withdrawal` — изъятие |
| 1343 | `cash_wd_amount` | сумма |
| 1354 | `cash_wd_comment` | комментарий |
| 1369 | `cash_wd_provesti` | проведение изъятия |

## Отмена уборки и отчёты

| Строка | Функция | Назначение |
|---|---|---|
| 1400 | `start_cancel_order` | `/cleaning_cancel_order N` — спросить подтверждение |
| 1424 | `cancel_order_confirmed` | откат уборки: `deleted_at` у заказа, мягкое удаление кассовых строк, возврат бонусов (`admin_ops.cancel_order`) |
| 1462 | `cleaning_cash_report` | `/cleaning_cash day\|month\|year` |
| 1486 | `cleaning_orders_list` | `/cleaning_orders day\|month` |
