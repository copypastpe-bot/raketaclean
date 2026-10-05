# Карта функций `cleaning/handlers.py`

1499 строк. Роутер aiogram со всеми диалогами клининга: проведение уборки,
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
| 204 | `_is_valid_phone` | 10-11 цифр в строке |
| 209 | `_money_str` | сумма без лишних нулей для сообщений |

## Доступ

| Строка | Функция | Назначение |
|---|---|---|
| 194 | `_is_foreman` | может ли этот Telegram-пользователь проводить уборки |
| 199 | `_has_permission` | проверка именного права (`cleaning_*`) |

## Очереди на сторону: уведомления и отчёт

| Строка | Функция | Назначение |
|---|---|---|
| 216 | `_enqueue_cleaning_completed_notifications` | ставит клиенту письма по правилам уведомлений и отмечает у уборки «попросили оценить» (`rating_requested_at`, прошлый ответ стирается) |
| 569 | `_enqueue_cleaning_order_report` | кладёт уборку в `pending_order_reports` (`kind='cleaning'`); отчёт уйдёт из `bot.py`, когда админ-бот заведёт связку с адресом |

## Проведение уборки (`CleaningOrderFSM`)

Порядок шагов: телефон → имя → комментарий → сумма → бонусы → способ оплаты →
сумма оплаты → расходы → подтверждение → проведение.

| Строка | Функция | Назначение |
|---|---|---|
| 282 | `start_cleaning_order` | вход: команда `/cleaning_order` или кнопка «🧹 Провести уборку» |
| 310 | `cancel` | «Отмена» на любом шаге |
| 316 | `got_phone` | телефон, поиск клиента, показ бонусов |
| 352 | `got_name` | имя клиента |
| 363 | `got_comment` | необязательный комментарий (сюда бригадир пишет адрес, если хочет) |
| 373 | `got_amount` | сумма чека |
| 391 | `got_bonus_spend` | сколько бонусов списать |
| 415 | `got_pay_method` | способ оплаты |
| 473 | `got_pay_amount` | сумма по выбранному способу (бывает несколько оплат) |
| 510 | `got_expense_category` | категория расхода или «Готово» |
| 524 | `got_expense_amount` | сумма расхода |
| 541 | `_show_confirm` | сводка перед проведением |
| 618 | `do_provesti` | **главный обработчик**: одна транзакция — клиент (623-633), приход (692), расходы (702), бонусы, уведомления клиенту (746), отчёт в очередь (777) |

## Баланс и поиск клиента

| Строка | Функция | Назначение |
|---|---|---|
| 819 | `cleaning_balance_cmd` | баланс кассы клининга (право `cleaning_view_balance`) |
| 833 | `cleaning_client_lookup_start` | кнопка «🔍 Клиент» (право `cleaning_view_clients`) |
| 844 | `cleaning_client_lookup_phone` | карточка клиента по телефону |

## Расход бригадира (`CleaningForemanExpenseFSM`)

| Строка | Функция | Назначение |
|---|---|---|
| 876 | `foreman_expense_start` | «➖ Добавить расход» (право `cleaning_record_expense`) |
| 887 | `foreman_expense_amount` | сумма |
| 898 | `foreman_expense_category` | категория |
| 916 | `foreman_expense_comment` | комментарий |
| 934 | `foreman_expense_confirm` | проведение и сообщение в кассу |

## Выплата прибыли (`CleaningDividendFSM`)

| Строка | Функция | Назначение |
|---|---|---|
| 968 | `_start_dividend` | общее начало: показать баланс, спросить сумму |
| 981 | `start_cleaning_dividend` | команда `/cleaning_dividend` (право `cleaning_manage_cash`) |
| 990 | `start_cleaning_payout_button` | текст «💸 Выплата» (право `cleaning_pay_dividend`; с 05.10 у клинера ни кнопки, ни права — только админы) |
| 999 | `div_amount` | сумма выплаты и расчёт долей |
| 1041 | `div_provesti` | проведение выплаты |
| 1089 | `start_dividend_cancel` | `/cleaning_dividend_cancel N` |
| 1123 | `dividend_cancel_confirmed` | отмена выплаты |

## Касса вручную

| Строка | Функция | Назначение |
|---|---|---|
| 1155 | `start_cash_add` | `/cleaning_cash_add` — приход (право `cleaning_manage_cash`) |
| 1175 | `cash_add_method` | способ |
| 1185 | `cash_add_amount` | сумма |
| 1196 | `cash_add_comment` | комментарий |
| 1211 | `cash_add_provesti` | проведение прихода |
| 1243 | `start_cash_expense` | `/cleaning_cash_expense` — расход |
| 1257 | `cash_exp_category` | категория |
| 1270 | `cash_exp_amount` | сумма |
| 1281 | `cash_exp_comment` | комментарий |
| 1296 | `cash_exp_provesti` | проведение расхода |
| 1328 | `start_cash_withdrawal` | `/cleaning_cash_withdrawal` — изъятие |
| 1342 | `cash_wd_amount` | сумма |
| 1353 | `cash_wd_comment` | комментарий |
| 1368 | `cash_wd_provesti` | проведение изъятия |

## Отмена уборки и отчёты

| Строка | Функция | Назначение |
|---|---|---|
| 1399 | `start_cancel_order` | `/cleaning_cancel_order N` — спросить подтверждение |
| 1423 | `cancel_order_confirmed` | откат уборки: `deleted_at` у заказа, мягкое удаление кассовых строк, возврат бонусов (`admin_ops.cancel_order`) |
| 1461 | `cleaning_cash_report` | `/cleaning_cash day\|month\|year` |
| 1485 | `cleaning_orders_list` | `/cleaning_orders day\|month` |
