"""Константы amoCRM аккаунта raketacleancrm (id 29605198).

Значения проверены разведкой 2026-08-24 — `tgbot-v1/recon/01-amocrm.md`.
Меняются только вместе с перепроверкой в интерфейсе амо.
"""

# --- Воронки ---
PIPELINE_PRIMARY = 4482751          # Воронка первичной обработки
PIPELINE_REALIZATION = 4482787      # Воронка для реализации
PIPELINE_CARPETS = 4645519          # Ковры Кристал (v1 их НЕ трогает)
PIPELINE_CARPETS_LEGACY = 5215336   # Воронка ковров реализация — рудимент, тоже не трогаем

# Воронки, которые робот игнорирует при поиске сделки: ковровые (заказ из бота
# ковровым быть не может — ковры приходят только из Excel партнёра) и архивные.
PIPELINE_ARCHIVED = (4482808, 4781380, 7554370)
PIPELINES_IGNORED = frozenset((PIPELINE_CARPETS, PIPELINE_CARPETS_LEGACY, *PIPELINE_ARCHIVED))

# --- Служебные этапы (одинаковы во всех воронках) ---
STATUS_SUCCESS = 142                # успешный этап любой воронки
STATUS_CLOSED = 143                 # «Закрыто и не реализовано»
STATUSES_FINAL = frozenset((STATUS_SUCCESS, STATUS_CLOSED))

# «Неразобранное» есть в каждой воронке под своим id. Это сырой след обращения
# (пропущенный звонок, заявка с сайта): кандидат последней очереди.
STATUS_UNSORTED_PRIMARY = 41463532
STATUS_UNSORTED_REALIZATION = 41463829
STATUSES_UNSORTED = frozenset((STATUS_UNSORTED_PRIMARY, STATUS_UNSORTED_REALIZATION))

# --- Этапы воронки первичной обработки ---
PRIM_STAGE_NEW_LEAD = 41463535       # Новый лид
PRIM_STAGE_CORRESPONDENCE = 41463538  # Переписка
PRIM_STAGE_NO_CONTACT = 41463541      # Не было 1-го касания
PRIM_STAGE_DIALOG_DONE = 41463544     # Диалог состоялся
PRIM_STAGE_DIALOG = 43489399          # Ведем Диалог

# --- Этапы воронки реализации ---
REAL_STAGE_CREATED = 41463832       # Заказ оформлен
REAL_STAGE_CONFIRMED = 41463838     # Заказ подтвержден, Мастер назначен
REAL_STAGE_DONE = 41463964          # Заказ выполнен

# --- Кастомные поля сделки ---
FIELD_SERVICE = 271915              # Услуга (multiselect)
FIELD_SPECIALIST = 39243            # Специалист (multiselect): мастер, выполнявший заказ
FIELD_ORDER_DATETIME = 18701        # Дата и время заказа (unix-время)
FIELD_ADDRESS = 18639               # Адрес

# --- Автозадачи ---
# Закрываем при проведении сделки (решение владельца №6).
TASK_TYPES_TO_CLOSE = frozenset((2270716, 2270737, 2270740, 2270743, 2301196))
# «Получить ОС» — закрываем ТОЛЬКО если клиент уже поставил оценку боту (решение №9).
TASK_TYPE_FEEDBACK = 2270746
