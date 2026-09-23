"""Объекты предметной области: заказ бота и привязка заказа к сделкам амо."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional


@dataclass(frozen=True)
class Order:
    """Закрытая мастером работа из БД рабочего бота (только чтение).

    Работ две разновидности, и лежат они в разных таблицах бота: заказ химчистки
    (`public.orders`) и уборка клининг-контура (`public.cleaning_orders`). Для
    движка и амо разницы почти нет — отличаются подпись владельцу (`label`) и то,
    что у уборки «Услуга» и «Специалист» заданы решением владельца, а не выведены
    из имени мастера.
    """

    order_id: int                     # «Заказ №N» = orders.id, отдельного счётчика нет
    phone10: Optional[str]            # 10 цифр номера клиента; None — номер не распознан
    created_at: datetime              # момент закрытия заказа мастером
    # Финальная сумма чека = бюджет сделки в амо (решение владельца №8, уточнено 2026-08-25).
    # Доп. продажу (orders.upsale_amount) сюда НЕ подмешиваем: в амо её нет,
    # она нужна только для расчёта зарплаты мастера внутри бота.
    amount_total: Decimal
    # Мастера заказа, основной первым: (имя, телефон). Телефон нужен, чтобы связать
    # мастера со «Специалистом» в амо — так различаются уборка и химчистка
    # одному клиенту в один день.
    masters: list[tuple[str, Optional[str]]] = field(default_factory=list)
    rating_score: Optional[int] = None   # оценка клиента: есть → задачу «Получить ОС» закрываем
    client_name: Optional[str] = None
    address: Optional[str] = None
    payment_method: Optional[str] = None   # «Наличные» | «Карта Дима» | «Карта Женя» | «р/с»
    # У клиента уже были заказы в боте. Нужно для «Источника сделки»:
    # первый раз — сарафан, дальше — «Повторный заказ».
    is_repeat_client: bool = False
    # Оплата по счёту ещё не поступила. Тогда сделку не проводим до конца, а оставляем
    # на «Заказ выполнен»: сейлзбот поставит задачу получить оплату (решение владельца).
    awaiting_wire_payment: bool = False
    # Откуда работа: "order" — химчистка из `orders`, "cleaning" — уборка из
    # `cleaning_orders`. Влияет на подпись владельцу и на то, в какой таблице
    # связок хранится ход работы (номера в этих таблицах пересекаются).
    kind: str = "order"
    # Переопределения на случай, когда вид работы известен заранее и выводить его
    # из имени мастера незачем: у уборки «Услуга» всегда «Уборка», а «Специалист»
    # всегда Ольга, кто бы ни был бригадиром (решение владельца 2026-09-10).
    # None — выводим по мастерам, как раньше.
    service_kind: Optional[str] = None
    specialist_enums: Optional[tuple[int, ...]] = None

    @property
    def order_date(self):
        """Дата заказа — по ней матчер сверяется со сделками амо."""
        return self.created_at.date()

    @property
    def label(self) -> str:
        """Как работа называется для владельца: «Заказ №5» или «Уборка №5».

        Подпись живёт в одном месте: её печатают карточки, отчёты о работе,
        предпросмотр хвоста и журнал движка — расходиться им нельзя.
        """
        return "Уборка" if self.kind == "cleaning" else "Заказ"

    @property
    def master_names(self) -> list[str]:
        return [name for name, _ in self.masters]


@dataclass(frozen=True)
class CarpetLink:
    """Строка adminbot.carpet_links: что робот знает о заказе партнёра по коврам.

    Ключ — номер заказа в CRM партнёра: он сквозной и приходит в каждом отчёте,
    поэтому месячный свод не приведёт к повторной обработке.
    """

    partner_id: int
    phone10: str
    status: str                       # new|in_progress|waiting_owner|waiting_salesbot|done|error
    # Как ведём заказ: сделка уже есть (None), через лид первичной («primary»)
    # или цепочкой с нуля («scratch»).
    path: Optional[str] = None
    lead_id: Optional[int] = None     # сделка ковровой воронки
    primary_lead_id: Optional[int] = None   # лид первичной, если цепочку вели с него
    checklist: dict[str, Any] = field(default_factory=dict)
    question: Optional[dict[str, Any]] = None
    question_msg_id: Optional[int] = None
    last_error: Optional[str] = None
    source_file: Optional[str] = None       # из какого вложения пришла строка
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class CalendarLink:
    """Строка adminbot.gcal_events: что робот знает о записи календаря.

    Ключ — идентификатор записи в Google: он вечный и переживает правки, поэтому
    повторный обмен по той же записи не заводит вторую сделку.
    """

    event_id: str
    kind: str                         # order|block|boat|rewash|unsettled|skip
    status: str                       # new|in_progress|waiting_salesbot|waiting_owner|
                                      # done|skipped|cancelled|error
    phone10: Optional[str] = None
    order_date: Optional[Any] = None  # date; заказ ведём по московской дате
    client_name: Optional[str] = None
    district: Optional[str] = None    # None — приставка заголовка непонятна
    services: tuple[str, ...] = ()
    event_data: Optional[dict[str, Any]] = None   # разбор записи: им продолжают цепочку
    skip_reason: Optional[str] = None
    path: Optional[str] = None        # A|B|C — как ведём запись
    primary_lead_id: Optional[int] = None
    real_lead_id: Optional[int] = None
    order_id: Optional[int] = None    # заказ бота, если он уже пришёл
    checklist: dict[str, Any] = field(default_factory=dict)
    question: Optional[dict[str, Any]] = None
    question_msg_id: Optional[int] = None
    done_msg_id: Optional[int] = None  # отчёт о работе отправлен — второй раз не пишем
    last_error: Optional[str] = None
    # Сверка «номер + имя» записи с контактом сделки (задача 5, ТЗ 2026-09-22):
    # то же устройство, что у напоминания про адрес (см. AmoLink ниже) — текст
    # расхождения человеческим языком, сколько раз уже напомнили, когда ушло
    # последнее и не пора ли молчать (кнопка «Я разобрался» или потолок в 7).
    contact_mismatch: Optional[str] = None
    contact_reminder_count: int = 0
    contact_reminder_sent_at: Optional[datetime] = None
    contact_reminder_muted: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class AutocallLead:
    """Строка adminbot.autocall_leads: цепочка попыток дозвона по заявке с сайта.

    Ключ — идентификатор сделки амо: наблюдатель видит одну и ту же заявку
    при каждом опросе, и вторая цепочка по той же сделке недопустима.
    """

    lead_id: int
    status: str                       # queued|calling|done|no_contact|gave_up|error
    phone10: Optional[str] = None
    attempts_total: int = 0
    manager_failures: int = 0
    client_failures: int = 0
    next_action_at: Optional[datetime] = None   # когда пора действовать; None — сразу
    call_id: Optional[str] = None     # идентификатор звонка в АТС
    called_at: Optional[datetime] = None        # когда отдали команду АТС
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class AmoLink:
    """Строка adminbot.amo_links: что робот знает и уже сделал по заказу."""

    order_id: int
    phone10: str
    status: str                       # new|in_progress|waiting_owner|waiting_salesbot|done|error
    path: Optional[str] = None        # A|B|C|D — путь заказа из дизайна §4
    primary_lead_id: Optional[int] = None
    real_lead_id: Optional[int] = None
    # Адрес сделки амо на момент, когда робот её заполнял (поле «Адрес»,
    # ПД — в логи не идёт). Пусто, если в сделке было пусто: не выдумываем
    # (ТЗ 2026-09-16, задача 1). Рабочий бот дозаполняет им три места у себя.
    deal_address: Optional[str] = None
    checklist: dict[str, Any] = field(default_factory=dict)   # шаг → время выполнения
    # Вопрос владельцу: причина и варианты сделок на выбор. Хранится рядом с заказом,
    # чтобы карточку можно было отправить (или переотправить) в любой момент.
    question: Optional[dict[str, Any]] = None
    question_msg_id: Optional[int] = None
    last_error: Optional[str] = None
    # Напоминание «сделка без адреса» (ТЗ 2026-09-16, задача 7): сколько раз
    # уже напомнили, когда ушло последнее (отсюда считаются сутки до следующего)
    # и не пора ли молчать — по воле владельца («Не напоминать») или своей,
    # дойдя до потолка в 7 штук.
    address_reminder_count: int = 0
    address_reminder_sent_at: Optional[datetime] = None
    address_reminder_muted: bool = False
    # Доводка сделки после оплаты по счёту (задача 11, ТЗ 2026-09-22):
    # `payment_pending` ставит основной движок в момент перевода сделки в
    # «Заказ выполнен», если оплата ещё не пришла; None — связка заведена до
    # этой миграции, и решает текущая стадия сделки. `payment_synced_at` —
    # когда робот довёл сделку до конца после прихода денег; None — ещё не
    # доводил, второй раз не берём.
    payment_pending: Optional[bool] = None
    payment_synced_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class DeletionRecord:
    """Удаление, ещё не разобранное: владелец снял заказ в рабочем боте.

    Два разных следа одного события (факт 1, ТЗ 2026-09-17 «удаление заказа
    освобождает сделку»): заказ химчистки пропадает физически и остаётся только
    строкой регистра `public.deleted_orders`, уборка остаётся строкой
    `public.cleaning_orders` с заполненным `deleted_at`. Разбор (задача 5, идёт
    отдельным этапом) у обоих один и тот же, поэтому источник приводит их
    к общему виду.

    Ключ разбора — `(kind, order_id)`, тот же, что в
    `adminbot.order_deletions_seen`: номера заказов и уборок пересекаются.
    """

    kind: str                              # 'order' | 'cleaning'
    order_id: int
    deleted_at: datetime
    client_id: Optional[int] = None
    phone_digits: Optional[str] = None     # для уборок в источнике нет — только у заказов химчистки
    amount_total: Optional[Decimal] = None
