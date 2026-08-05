"""
Telegram Bot for Supply & Payment Accounting
Version 7.0.0 - Migrated from Google Sheets to Supabase (Postgres)
"""
import os
import re
import json
import logging
import httpx
from datetime import datetime, timedelta
import zoneinfo
from dotenv import load_dotenv

load_dotenv()

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters, CallbackQueryHandler
)
from supabase import create_client, Client

# --- CONFIGURATION ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")  # service_role key — не публиковать нигде
DATA_DIR = os.getenv("DATA_DIR", "/app/data")  # постоянный том — переживает перезапуски контейнера

# Ozon Seller API — ИП Булат (первый подключённый кабинет)
OZON_BULAT_CLIENT_ID = os.getenv("OZON_BULAT_CLIENT_ID", "")
OZON_BULAT_API_KEY = os.getenv("OZON_BULAT_API_KEY", "")
OZON_API_BASE = "https://api-seller.ozon.ru"

# Anthropic (Claude) API — генерация ответов на отзывы/вопросы Ozon
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
ANTHROPIC_API_BASE = "https://api.anthropic.com/v1/messages"

# Как часто фоново проверять новые отзывы/вопросы Ozon (минуты)
OZON_FEEDBACK_SYNC_MINUTES = int(os.getenv("OZON_FEEDBACK_SYNC_MINUTES", "45"))

# ВРЕМЕННЫЙ ПОДБОР: допустимые значения filter.states для /v3/supply-order/list ещё не найдены в документации.
# Меняйте это значение прямо в переменных окружения Bothost между попытками (без нового деплоя кода) —
# один перезапуск процесса вместо полного цикла код -> PR -> мерж -> деплой. Убрать после того, как найдём
# реальный диапазон допустимых значений states.
OZON_SUPPLY_STATES_PROBE = [int(x) for x in os.getenv("OZON_SUPPLY_STATES_PROBE", "1").split(",") if x.strip()]

TZ_MSK = zoneinfo.ZoneInfo("Europe/Moscow")

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

RULES_TEXT = (
    "🛑 *ПРАВИЛА СИСТЕМЫ:*\n"
    "1️⃣ Числа вводим без букв (просто `500`).\n"
    "2️⃣ Кнопка «❌ Главное меню» отменяет любой шаг.\n"
)

# Категория расхода — определяется автоматически по типу контрагента
CATEGORY_BY_COUNTERPARTY_TYPE = {
    "поставщик сырья": "Закупка сырья (орехи/сухофрукты)",
    "поставщик расходников": "Расходники (упаковка, скотч, этикетки)",
    "сотрудник": "Зарплата",
}
DEFAULT_CATEGORY = "Прочие расходы"
WAREHOUSE_IP_LABEL = "Склад/Производство"  # служебная метка для закупок сырья — не входит в справочник «Наши ИП»
MEAL_COMP_HOURS_THRESHOLD = 8   # смена от 8 часов — компенсация обеда
MEAL_COMP_AMOUNT = 350
LABOR_COST_PER_UNIT = 5  # фиксированная оценка труда фасовки на 1 упаковку, ₽

# Кнопки выбора типа — единая карта на все три раздела (Закупка/Оплата/История)
TYPE_BUTTONS_SUPPLY = ["🌰 Сырьё", "📦 Расходники"]
TYPE_BUTTONS_FULL = ["🌰 Сырьё", "📦 Расходники", "👤 Сотрудники"]
TYPE_BUTTON_TO_DB = {
    "🌰 Сырьё": "поставщик сырья",
    "📦 Расходники": "поставщик расходников",
    "👤 Сотрудники": "сотрудник",
}


# --- SUPABASE SERVICE ---
class SupabaseService:
    def __init__(self):
        self.client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        self._category_cache = {}
        self._ip_cache = {}
        self._account_cache = {}
        self._load_caches()

    def _load_caches(self):
        cats = self.client.table("categories").select("id,name").execute().data or []
        self._category_cache = {c["name"]: c["id"] for c in cats}
        ips = self.client.table("ip").select("id,name").execute().data or []
        self._ip_cache = {i["name"]: i["id"] for i in ips}
        accs = self.client.table("accounts").select("id,code").execute().data or []
        self._account_cache = {a["code"]: a["id"] for a in accs}

    def refresh_ip_cache(self):
        self._load_caches()

    def get_category_id(self, name: str):
        return self._category_cache.get(name)

    def get_ip_id(self, name: str):
        return self._ip_cache.get(name)

    def get_account_id(self, code: str):
        return self._account_cache.get(code)

    def post_journal_entry(self, operation_id, entry_date, debit_code, credit_code, amount, comment=""):
        """Двойная запись: один дебет + один кредит на одну и ту же сумму. Невидимо для пользователя бота."""
        self.client.table("journal_entries").insert({
            "operation_id": operation_id,
            "entry_date": entry_date,
            "debit_account_id": self.get_account_id(debit_code),
            "credit_account_id": self.get_account_id(credit_code),
            "amount": amount,
            "comment": comment,
        }).execute()

    def get_journal_entry_for_operation(self, operation_id: int):
        res = self.client.table("journal_entries").select("*").eq("operation_id", operation_id).limit(1).execute()
        data = res.data or []
        return data[0] if data else None

    def get_account_code_by_id(self, account_id: int):
        for code, aid in self._account_cache.items():
            if aid == account_id:
                return code
        return None

    def get_trial_balance(self):
        """Классический пробный баланс: обороты по дебету и кредиту каждого счёта."""
        entries = self.client.table("journal_entries").select("debit_account_id,credit_account_id,amount").execute().data or []
        accounts = self.client.table("accounts").select("id,code,name").order("code").execute().data or []
        acc_map = {a["id"]: a for a in accounts}

        totals = {a["id"]: {"debit": 0.0, "credit": 0.0, "code": a["code"], "name": a["name"]} for a in accounts}
        for e in entries:
            amt = float(e["amount"])
            if e["debit_account_id"] in totals:
                totals[e["debit_account_id"]]["debit"] += amt
            if e["credit_account_id"] in totals:
                totals[e["credit_account_id"]]["credit"] += amt

        result = [v for v in totals.values() if v["debit"] or v["credit"]]
        result.sort(key=lambda x: x["code"])
        return result

    # --- Reference data ---
    def get_suppliers_list(self, type_filter: str = None):
        """Контрагенты. Если указан type_filter — только контрагенты этого типа."""
        q = self.client.table("counterparties").select("id,name,phone,type")
        if type_filter:
            q = q.eq("type", type_filter)
        res = q.order("name").execute()
        return res.data or []

    def get_consumables_list(self):
        res = self.client.table("consumables_catalog").select("id,name,size").order("name").execute()
        items = []
        for r in (res.data or []):
            items.append(f"{r['name']} {r['size']}" if r.get("size") else r["name"])
        return items

    def get_history(self, counterparty_id: int):
        res = (
            self.client.table("operations")
            .select("*")
            .eq("counterparty_id", counterparty_id)
            .order("operation_date")
            .order("id")
            .execute()
        )
        return res.data or []

    def get_all_product_names(self):
        """Названия сырья/товаров по всем ИП сразу, без привязки — для прихода на склад."""
        res = self.client.table("products").select("name").order("name").execute()
        seen, names = set(), []
        for r in (res.data or []):
            n = r["name"]
            if n not in seen:
                seen.add(n)
                names.append(n)
        return names

    def upsert_warehouse_movements_batch(self, rows: list):
        """Пакетное списание со склада по факту реальных продаж Ozon — идемпотентно (по ozon_operation_id)."""
        if not rows:
            return
        self.client.table("warehouse_movements").upsert(rows, on_conflict="ozon_operation_id").execute()

    def add_warehouse_movement(self, **fields):
        self.client.table("warehouse_movements").insert(fields).execute()

    def get_stock_balances(self):
        """Остаток = сумма приходов минус сумма расходов, по каждому наименованию."""
        res = self.client.table("warehouse_movements").select("product_name,direction,quantity").execute()
        balances = {}
        for r in (res.data or []):
            name = r["product_name"]
            qty = float(r["quantity"])
            delta = qty if r["direction"] == "приход" else -qty
            balances[name] = balances.get(name, 0.0) + delta
        return sorted(balances.items())

    def get_my_ip_list(self):
        res = self.client.table("ip").select("name").eq("is_active", True).order("name").execute()
        return [r["name"] for r in res.data or []]

    def get_products_list(self, ip_name: str = None):
        q = self.client.table("products").select("id,name,ip_id")
        if ip_name:
            ip_id = self.get_ip_id(ip_name)
            if ip_id:
                q = q.eq("ip_id", ip_id)
        res = q.order("name").execute()
        return [r["name"] for r in res.data or []]

    def get_counterparty_by_name(self, name: str):
        res = self.client.table("counterparties").select("*").eq("name", name).limit(1).execute()
        data = res.data or []
        return data[0] if data else None

    def get_supplier_current_debt(self, supplier_name: str) -> float:
        cp = self.get_counterparty_by_name(supplier_name)
        if not cp:
            return 0.0
        res = self.client.table("operations").select("amount").eq("counterparty_id", cp["id"]).execute()
        total = sum(float(r["amount"]) for r in (res.data or []))
        return round(total, 2)

    # --- Writing operations ---
    def add_operation(self, **fields):
        self.client.table("operations").insert(fields).execute()

    def add_operation_returning_id(self, **fields):
        res = self.client.table("operations").insert(fields).execute()
        return res.data[0]["id"]

    def mark_operation_reversed(self, original_id: int, reversal_id: int):
        self.client.table("operations").update({"reversed_by": reversal_id}).eq("id", original_id).execute()

    def add_counterparty(self, name: str, phone: str = None, type_: str = "поставщик сырья"):
        self.client.table("counterparties").insert({"name": name, "phone": phone, "type": type_}).execute()

    def add_product(self, name: str, ip_id: int = None, unit: str = "шт"):
        self.client.table("products").insert({"name": name, "ip_id": ip_id, "unit": unit}).execute()

    def get_last_operations(self, limit: int = 7):
        res = (
            self.client.table("operations")
            .select("*, counterparties(name)")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []

    def get_all_debts(self):
        """Баланс по каждому контрагенту сразу — для сводного отчёта по всем долгам."""
        ops = self.client.table("operations").select("counterparty_id,amount").execute().data or []
        sums = {}
        for r in ops:
            cid = r.get("counterparty_id")
            if cid is None:
                continue
            sums[cid] = sums.get(cid, 0.0) + float(r["amount"])

        nonzero_ids = [cid for cid, total in sums.items() if abs(total) >= 0.01]
        if not nonzero_ids:
            return []

        cps = self.client.table("counterparties").select("id,name,type").in_("id", nonzero_ids).execute().data or []
        cp_map = {c["id"]: c for c in cps}

        result = []
        for cid in nonzero_ids:
            cp = cp_map.get(cid, {})
            result.append({
                "name": cp.get("name", "?"),
                "type": cp.get("type", ""),
                "balance": round(sums[cid], 2),
            })
        result.sort(key=lambda x: -abs(x["balance"]))
        return result

    def add_sale(self, **fields):
        res = self.client.table("sales").insert(fields).execute()
        return res.data[0]["id"]

    def get_material_cost_per_kg(self, product_name: str):
        """
        Скользящая средневзвешенная себестоимость (Moving Weighted Average).
        Пересчитывается только на приходах, смешивая новую партию с тем, что ЕЩЁ реально
        осталось на складе — а не со всей историей закупок. Расход (отгрузка/продажа)
        просто уменьшает остаток, не влияя на среднюю цену.
        """
        res = (
            self.client.table("warehouse_movements")
            .select("direction,quantity,unit_price,movement_date,id")
            .eq("product_name", product_name)
            .order("movement_date")
            .order("id")
            .execute()
        )
        rows = res.data or []
        qty_on_hand = 0.0
        avg_cost = 0.0
        for r in rows:
            qty = float(r["quantity"])
            if r["direction"] == "приход":
                price = r.get("unit_price")
                if price is None:
                    continue  # старые записи без цены — пропускаем, не искажаем среднюю
                price = float(price)
                new_qty = qty_on_hand + qty
                avg_cost = (qty_on_hand * avg_cost + qty * price) / new_qty if new_qty > 0 else price
                qty_on_hand = new_qty
            else:  # расход
                qty_on_hand = max(0.0, qty_on_hand - qty)
        return round(avg_cost, 2) if avg_cost else None

    def get_unit_cost_components(self, product_name: str, packaging: str):
        """Труд/расходники/аренда/логистика на 1 упаковку.
        Ищем в порядке точности: (товар+фасовка) -> (товар, любая фасовка) -> общее значение по умолчанию."""
        res = self.client.table("unit_costs").select("*").eq("product_name", product_name).eq("packaging", packaging).limit(1).execute()
        rows = res.data or []
        if not rows:
            res = self.client.table("unit_costs").select("*").eq("product_name", product_name).is_("packaging", "null").limit(1).execute()
            rows = res.data or []
        if not rows:
            res = self.client.table("unit_costs").select("*").is_("product_name", "null").is_("packaging", "null").limit(1).execute()
            rows = res.data or []
        return rows[0] if rows else None

    def get_period_pnl(self, date_from: str, date_to: str):
        """
        Настоящий P&L за период:
        - Выручка = реальные поступления от площадок (когда деньги пришли на счёт)
        - Себестоимость = списана в момент ОТГРУЗКИ (проводки на счёт 5950), а не в момент закупки
        - Закупка сырья/расходников сама по себе НЕ расход — это просто превращение денег в запасы
        - Прочие периодные расходы (зарплата, аренда, подписки) — как обычно, по факту начисления
        """
        # Выручка — только реальные поступления от площадок
        rev_ops = (
            self.client.table("operations").select("amount")
            .eq("operation_type", "поступление")
            .gte("operation_date", date_from).lte("operation_date", date_to)
            .execute().data or []
        )
        revenue = sum(float(r["amount"]) for r in rev_ops)

        # Себестоимость реализованной продукции — из проводок, списанных при отгрузке
        cogs_account_id = self.get_account_id("5950")
        cogs_entries = (
            self.client.table("journal_entries").select("amount")
            .eq("debit_account_id", cogs_account_id)
            .gte("entry_date", date_from).lte("entry_date", date_to)
            .execute().data or []
        )
        cogs_total = sum(float(e["amount"]) for e in cogs_entries)

        # Прочие расходы периода — зарплата и постоянные расходы (НЕ закупка сырья/расходников)
        ops = (
            self.client.table("operations")
            .select("amount,category_id,operation_type,operation_date")
            .in_("operation_type", ["начисление", "расход"])
            .gte("operation_date", date_from)
            .lte("operation_date", date_to)
            .execute().data or []
        )
        cats = self.client.table("categories").select("id,name").execute().data or []
        cat_map = {c["id"]: c["name"] for c in cats}

        by_category = {"Себестоимость реализованной продукции": cogs_total}
        total_expenses = cogs_total
        for r in ops:
            name = cat_map.get(r["category_id"], "Без категории")
            amt = float(r["amount"])
            by_category[name] = by_category.get(name, 0.0) + amt
            total_expenses += amt

        return {
            "revenue": round(revenue, 2),
            "expenses_by_category": sorted(by_category.items(), key=lambda x: -x[1]),
            "total_expenses": round(total_expenses, 2),
            "net_profit": round(revenue - total_expenses, 2),
        }

    def upsert_ozon_transaction(self, **fields):
        self.client.table("ozon_transactions").upsert(fields, on_conflict="ozon_operation_id").execute()

    def upsert_ozon_transactions_batch(self, rows: list):
        """Пакетная запись — одним запросом вместо тысяч отдельных, чтобы не рвать соединение."""
        if not rows:
            return
        self.client.table("ozon_transactions").upsert(rows, on_conflict="ozon_operation_id").execute()

    # --- Ozon отзывы/вопросы ---
    def get_existing_ozon_feedback_ids(self, ozon_ids: list) -> set:
        """Какие из этих ozon_id уже есть в базе — чтобы не вставлять дубли и не затирать статус уже обработанных."""
        if not ozon_ids:
            return set()
        res = self.client.table("ozon_feedback").select("ozon_id").in_("ozon_id", ozon_ids).execute()
        return {r["ozon_id"] for r in (res.data or [])}

    def insert_ozon_feedback_batch(self, rows: list):
        """Вставка ТОЛЬКО новых записей (status='new') — вызывающий код уже отфильтровал дубли по ozon_id."""
        if not rows:
            return
        self.client.table("ozon_feedback").insert(rows).execute()

    def get_new_ozon_feedback(self):
        res = self.client.table("ozon_feedback").select("*").eq("status", "new").execute()
        return res.data or []

    def update_ozon_feedback(self, feedback_id, **fields):
        self.client.table("ozon_feedback").update(fields).eq("id", feedback_id).execute()

    def get_ozon_feedback(self, feedback_id):
        res = self.client.table("ozon_feedback").select("*").eq("id", feedback_id).limit(1).execute()
        data = res.data or []
        return data[0] if data else None

    # --- Ozon приёмка поставок (списание склада по факту) ---
    def get_existing_supply_acceptance_keys(self, supply_numbers: list) -> set:
        """Какие (supply_number, sku) уже обработаны — составной ключ, у Supabase REST нет .in_() по двум колонкам сразу."""
        if not supply_numbers:
            return set()
        res = self.client.table("ozon_supply_acceptances").select("supply_number,sku").in_("supply_number", supply_numbers).execute()
        return {(r["supply_number"], r["sku"]) for r in (res.data or [])}

    def add_warehouse_movements_batch(self, rows: list):
        """Один атомарный INSERT на всю поставку/sku — либо все строки списания проходят, либо ни одной."""
        if not rows:
            return
        self.client.table("warehouse_movements").insert(rows).execute()

    def insert_supply_acceptance(self, **fields):
        self.client.table("ozon_supply_acceptances").insert(fields).execute()

    def get_product_recipe(self, product_name: str) -> list:
        res = self.client.table("product_recipes").select("consumable_name,qty_per_unit").eq("product_name", product_name).execute()
        return res.data or []

    def get_warehouse_activity_history(self):
        """Единая история всех действий по складу: постоянные расходы, логистика, отгрузки — по датам вместе."""
        items = []

        ops = (
            self.client.table("operations").select("operation_date,amount,payment_method,comment,category_id")
            .is_("counterparty_id", "null")
            .execute().data or []
        )
        cats = self.client.table("categories").select("id,name").execute().data or []
        cat_map = {c["id"]: c["name"] for c in cats}
        logistics_cat_id = self.get_category_id("Логистика до маркетплейса")

        for r in ops:
            cat_name = cat_map.get(r.get("category_id"), "")
            icon = "🚚" if r.get("category_id") == logistics_cat_id else "💸"
            items.append({
                "date": r.get("operation_date", ""),
                "label": f"{icon} {cat_name}",
                "detail": f"{r.get('comment','')} — {r['amount']} ₽",
            })

        moves = (
            self.client.table("warehouse_movements").select("movement_date,product_name,quantity,packaging,marketplace,direction")
            .eq("direction", "расход")
            .execute().data or []
        )
        for m in moves:
            items.append({
                "date": m.get("movement_date", ""),
                "label": "📤 Отгрузка",
                "detail": f"{m.get('product_name','')} ({m.get('packaging') or ''}) {m.get('quantity')}кг → {m.get('marketplace') or ''}",
            })

        items.sort(key=lambda x: x["date"])
        return items

    def get_full_period_profit(self, date_from: str, date_to: str):
        """
        Настоящая прибыль за период — только по фактическим данным:
        - Выручка = реальные суммы из Ozon (уже после комиссии/эквайринга/логистики площадки/рекламы)
        - Себестоимость = реальная средневзвешенная цена сырья × реально проданные штуки
        - Расходы = реальные начисления/расходы (зарплата, аренда, логистика до МП и т.д.), без отменённых сторно
        - Налог (1%) и доля Булата (2%) считаются автоматически от суммы поступлений
        """
        # Supabase по умолчанию отдаёт максимум 1000 строк за раз — для целого месяца строк может быть
        # больше 10 000, поэтому явно постранично забираем всё, а не одним запросом.
        ozon_rows = []
        page_size = 1000
        offset = 0
        while True:
            chunk = (
                self.client.table("ozon_transactions").select("amount,item_name,operation_type_name")
                .gte("operation_date", date_from).lte("operation_date", date_to)
                .range(offset, offset + page_size - 1)
                .execute().data or []
            )
            ozon_rows.extend(chunk)
            if len(chunk) < page_size:
                break
            offset += page_size
        revenue = round(sum(float(r["amount"]) for r in ozon_rows), 2)

        # Штук по товару (только реальные продажи, тип "Доставка покупателю")
        units_by_product = {}
        for r in ozon_rows:
            if r.get("operation_type_name") == "Доставка покупателю" and r.get("item_name"):
                units_by_product[r["item_name"]] = units_by_product.get(r["item_name"], 0) + 1

        # Себестоимость по каждому товару — через сопоставление названия Ozon с нашими товарами по ключевым словам
        products = self.client.table("products").select("name").execute().data or []
        product_names = [p["name"] for p in products]

        cogs_total = 0.0
        packaging_total = 0.0
        product_breakdown = []
        for ozon_name, units in units_by_product.items():
            matched = next((p for p in product_names if p.lower() in ozon_name.lower()), None)
            cost_per_kg = self.get_material_cost_per_kg(matched) if matched else None
            packaging_per_unit, _ = self.get_packaging_cost_per_unit(matched) if matched else (None, [])
            item_revenue = round(sum(float(r["amount"]) for r in ozon_rows if r.get("item_name") == ozon_name), 2)
            item_cogs = round(cost_per_kg * units, 2) if cost_per_kg else 0.0
            item_packaging = round((packaging_per_unit or 0) * units, 2)
            cogs_total += item_cogs
            packaging_total += item_packaging
            product_breakdown.append({
                "name": matched or ozon_name, "units": units,
                "revenue": item_revenue, "cogs": item_cogs, "packaging": item_packaging,
                "margin": round(item_revenue - item_cogs - item_packaging, 2),
            })
        product_breakdown.sort(key=lambda x: -x["revenue"])
        cogs_total = round(cogs_total, 2)
        packaging_total = round(packaging_total, 2)

        # Реальные расходы (зарплата, постоянные расходы, логистика до МП) — без отменённых сторно
        ops = (
            self.client.table("operations").select("amount,category_id,operation_type")
            .in_("operation_type", ["начисление", "расход"])
            .is_("reversed_by", "null")
            .gte("operation_date", date_from).lte("operation_date", date_to)
            .execute().data or []
        )
        cats = self.client.table("categories").select("id,name").execute().data or []
        cat_map = {c["id"]: c["name"] for c in cats}
        expenses_by_category = {}
        expenses_total = 0.0
        for r in ops:
            name = cat_map.get(r["category_id"], "Без категории")
            amt = float(r["amount"])
            expenses_by_category[name] = expenses_by_category.get(name, 0.0) + amt
            expenses_total += amt

        tax = round(revenue * 0.01, 2)
        bulat_share = round(revenue * 0.02, 2)

        net_profit = round(revenue - cogs_total - packaging_total - expenses_total - tax - bulat_share, 2)

        return {
            "revenue": revenue,
            "cogs_total": cogs_total,
            "packaging_total": packaging_total,
            "product_breakdown": product_breakdown,
            "expenses_by_category": sorted(expenses_by_category.items(), key=lambda x: -x[1]),
            "expenses_total": round(expenses_total, 2),
            "tax": tax,
            "bulat_share": bulat_share,
            "net_profit": net_profit,
        }

    def get_packaging_cost_per_unit(self, product_name: str):
        """
        Реальная себестоимость расходников на 1 штуку — считается по рецептуре (product_recipes)
        и реальной средневзвешенной цене каждого расходника (из фактических закупок).
        """
        recipe = self.client.table("product_recipes").select("consumable_name,qty_per_unit").eq("product_name", product_name).execute().data or []
        if not recipe:
            return None, []
        total = 0.0
        breakdown = []
        for r in recipe:
            price = self.get_material_cost_per_kg(r["consumable_name"])  # универсальная средневзвешенная, работает для любых названий
            if price is None:
                continue
            cost = round(price * float(r["qty_per_unit"]), 4)
            total += cost
            breakdown.append({"name": r["consumable_name"], "qty": r["qty_per_unit"], "price": price, "cost": cost})
        return round(total, 2), breakdown

    def get_logistics_history(self):
        cat_id = self.get_category_id("Логистика до маркетплейса")
        res = (
            self.client.table("operations").select("*")
            .eq("category_id", cat_id)
            .order("operation_date").order("id")
            .execute()
        )
        return res.data or []

    def get_hourly_employees(self):
        """Сотрудники с почасовой ставкой (для учёта смен)."""
        res = self.client.table("counterparties").select("id,name,hourly_rate").eq("type", "сотрудник").not_.is_("hourly_rate", "null").order("name").execute()
        return res.data or []

    def get_all_employees(self):
        """Все сотрудники (для начисления фиксированного оклада)."""
        res = self.client.table("counterparties").select("id,name,hourly_rate").eq("type", "сотрудник").order("name").execute()
        return res.data or []

    # --- Access control ---
    def is_approved(self, user_id: int) -> bool:
        res = self.client.table("approved_users").select("user_id").eq("user_id", user_id).execute()
        return len(res.data or []) > 0

    def approve_user(self, user_id: int, name: str, phone: str):
        self.client.table("approved_users").insert({"user_id": user_id, "name": name, "phone": phone}).execute()
        self.client.table("pending_approvals").update({"status": "approved"}).eq("user_id", user_id).eq(
            "status", "pending"
        ).execute()

    def add_pending(self, user_id: int, phone: str, name: str):
        self.client.table("pending_approvals").insert({"user_id": user_id, "phone": phone, "name": name}).execute()

    def is_pending(self, user_id: int) -> bool:
        res = self.client.table("pending_approvals").select("id").eq("user_id", user_id).eq("status", "pending").execute()
        return len(res.data or []) > 0

    # --- Reminders ---
    def add_reminder(self, remind_at_iso: str, category: str, description: str):
        self.client.table("reminders").insert(
            {"remind_at": remind_at_iso, "category": category, "description": description}
        ).execute()


def infer_category_name(counterparty_type: str) -> str:
    return CATEGORY_BY_COUNTERPARTY_TYPE.get(counterparty_type, DEFAULT_CATEGORY)


# --- STATES ---
SUPPLY_CATEGORY = "SUPPLY_CATEGORY"
SUPPLY_SUPPLIER = "SUPPLY_SUPPLIER"
SUPPLY_MY_IP = "SUPPLY_MY_IP"
SUPPLY_PRODUCT = "SUPPLY_PRODUCT"
SUPPLY_QTY = "SUPPLY_QTY"
SUPPLY_PRICE = "SUPPLY_PRICE"
SUPPLY_UNIT = "SUPPLY_UNIT"
SUPPLY_COMMENT = "SUPPLY_COMMENT"
SUPPLY_CONFIRM = "SUPPLY_CONFIRM"
SUPPLY_ADD_MORE = "SUPPLY_ADD_MORE"
PAYMENT_CATEGORY = "PAYMENT_CATEGORY"
PAYMENT_SUPPLIER = "PAYMENT_SUPPLIER"
PAYMENT_AMOUNT = "PAYMENT_AMOUNT"
PAYMENT_TYPE = "PAYMENT_TYPE"
PAYMENT_COMMENT = "PAYMENT_COMMENT"
PAYMENT_CONFIRM = "PAYMENT_CONFIRM"
ADD_SELECT = "ADD_SELECT"
ADD_SUPPLIER_PHONE = "ADD_SUPPLIER_PHONE"
ADD_SUPPLIER_NAME = "ADD_SUPPLIER_NAME"
ADD_SUPPLIER_TYPE = "ADD_SUPPLIER_TYPE"
ADD_SUPPLIER_CONFIRM = "ADD_SUPPLIER_CONFIRM"
ADD_MY_IP_NAME = "ADD_MY_IP_NAME"
ADD_MY_IP_CONFIRM = "ADD_MY_IP_CONFIRM"
ADD_PRODUCT_NAME = "ADD_PRODUCT_NAME"
ADD_PRODUCT_IP = "ADD_PRODUCT_IP"
REMINDER_TYPE_SELECT = "REMINDER_TYPE_SELECT"
REMINDER_INPUT_FLOW = "REMINDER_INPUT_FLOW"
REMINDER_DATE_SELECT = "REMINDER_DATE_SELECT"
REMINDER_TIME_SELECT = "REMINDER_TIME_SELECT"
BALANCE_SUPPLIER = "BALANCE_SUPPLIER"
BALANCE_MODE = "BALANCE_MODE"
HISTORY_CATEGORY = "HISTORY_CATEGORY"
HISTORY_SUPPLIER = "HISTORY_SUPPLIER"
HISTORY_REVERSE_SELECT = "HISTORY_REVERSE_SELECT"
HISTORY_REVERSE_NUMBER = "HISTORY_REVERSE_NUMBER"
HISTORY_REVERSE_CONFIRM = "HISTORY_REVERSE_CONFIRM"
WAREHOUSE_MENU = "WAREHOUSE_MENU"
WAREHOUSE_IN_SUPPLIER = "WAREHOUSE_IN_SUPPLIER"
WAREHOUSE_IN_PRODUCT = "WAREHOUSE_IN_PRODUCT"
WAREHOUSE_IN_QTY = "WAREHOUSE_IN_QTY"
WAREHOUSE_IN_COMMENT = "WAREHOUSE_IN_COMMENT"
WAREHOUSE_IN_CONFIRM = "WAREHOUSE_IN_CONFIRM"
WAREHOUSE_OUT_IP = "WAREHOUSE_OUT_IP"
WAREHOUSE_OUT_PRODUCT = "WAREHOUSE_OUT_PRODUCT"
WAREHOUSE_OUT_PACKAGING = "WAREHOUSE_OUT_PACKAGING"
WAREHOUSE_OUT_QTY = "WAREHOUSE_OUT_QTY"
WAREHOUSE_OUT_ADD_MORE = "WAREHOUSE_OUT_ADD_MORE"
WAREHOUSE_OUT_MARKETPLACE = "WAREHOUSE_OUT_MARKETPLACE"
WAREHOUSE_OUT_CONFIRM = "WAREHOUSE_OUT_CONFIRM"
WAREHOUSE_EXPENSE_CATEGORY = "WAREHOUSE_EXPENSE_CATEGORY"
WAREHOUSE_EXPENSE_PAYMENT = "WAREHOUSE_EXPENSE_PAYMENT"
WAREHOUSE_EXPENSE_AMOUNT = "WAREHOUSE_EXPENSE_AMOUNT"
WAREHOUSE_EXPENSE_COMMENT = "WAREHOUSE_EXPENSE_COMMENT"
WAREHOUSE_EXPENSE_CONFIRM = "WAREHOUSE_EXPENSE_CONFIRM"
LOGISTICS_MENU = "LOGISTICS_MENU"
LOGISTICS_MARKETPLACE = "LOGISTICS_MARKETPLACE"
LOGISTICS_QTY = "LOGISTICS_QTY"
LOGISTICS_AMOUNT = "LOGISTICS_AMOUNT"
LOGISTICS_PAYMENT = "LOGISTICS_PAYMENT"
LOGISTICS_COMMENT = "LOGISTICS_COMMENT"
LOGISTICS_CONFIRM = "LOGISTICS_CONFIRM"
EMPLOYEE_MENU = "EMPLOYEE_MENU"
EMP_SHIFT_EMPLOYEE = "EMP_SHIFT_EMPLOYEE"
EMP_SHIFT_DATE = "EMP_SHIFT_DATE"
EMP_SHIFT_START = "EMP_SHIFT_START"
EMP_SHIFT_END = "EMP_SHIFT_END"
EMP_SHIFT_CONFIRM = "EMP_SHIFT_CONFIRM"
EMP_SHIFT_NEXT = "EMP_SHIFT_NEXT"
EMP_ACCRUAL_EMPLOYEE = "EMP_ACCRUAL_EMPLOYEE"
EMP_ACCRUAL_AMOUNT = "EMP_ACCRUAL_AMOUNT"
EMP_ACCRUAL_COMMENT = "EMP_ACCRUAL_COMMENT"
EMP_ACCRUAL_CONFIRM = "EMP_ACCRUAL_CONFIRM"
SALE_IP = "SALE_IP"
SALE_MARKETPLACE = "SALE_MARKETPLACE"
SALE_PRODUCT = "SALE_PRODUCT"
SALE_PACKAGING = "SALE_PACKAGING"
SALE_UNITS = "SALE_UNITS"
SALE_PRICE = "SALE_PRICE"
SALE_ADD_MORE = "SALE_ADD_MORE"
SALE_COMMISSION = "SALE_COMMISSION"
SALE_CONFIRM = "SALE_CONFIRM"
PROFIT_MODE = "PROFIT_MODE"
PROFIT_PRODUCT = "PROFIT_PRODUCT"
PROFIT_PACKAGING = "PROFIT_PACKAGING"
PROFIT_MARKETPLACE = "PROFIT_MARKETPLACE"
PROFIT_PERIOD = "PROFIT_PERIOD"
PROFIT_PERIOD_CUSTOM = "PROFIT_PERIOD_CUSTOM"
OZON_SYNC_PERIOD = "OZON_SYNC_PERIOD"
OZON_SYNC_PERIOD_CUSTOM = "OZON_SYNC_PERIOD_CUSTOM"
OZON_FEEDBACK_SYNC_PERIOD = "OZON_FEEDBACK_SYNC_PERIOD"
OZON_FEEDBACK_SYNC_PERIOD_CUSTOM = "OZON_FEEDBACK_SYNC_PERIOD_CUSTOM"
FEEDBACK_EDIT_TEXT = "FEEDBACK_EDIT_TEXT"
OZON_SUPPLY_SYNC_PERIOD = "OZON_SUPPLY_SYNC_PERIOD"
OZON_SUPPLY_SYNC_PERIOD_CUSTOM = "OZON_SUPPLY_SYNC_PERIOD_CUSTOM"



# --- SMART GRID KEYBOARD BUILDER ---
def build_grid_keyboard(buttons_list, columns=2, add_navigation=True):
    grid = []
    row = []
    seen = set()
    clean_buttons = [x for x in buttons_list if not (x in seen or seen.add(x))]

    for btn in clean_buttons:
        row.append(str(btn))
        if len(row) == columns:
            grid.append(row)
            row = []
    if row:
        grid.append(row)

    if add_navigation:
        grid.append(["🔙 Назад", "❌ Главное меню"])
    return ReplyKeyboardMarkup(grid, resize_keyboard=True)


def get_main_menu_keyboard(user_id):
    kb = [["📦 Закупка", "💰 Оплата"], ["🏭 Склад", "💵 Продажа"], ["📜 История", "📊 Баланс"], ["➕ Добавить", "❓ Помощь"]]
    if user_id == ADMIN_ID:
        kb[3].append("⏰ Напомнить")
        kb.append(["🔄 Синхр. Ozon", "🔄 Синхр. отзывы"])
        kb.append(["📦 Приёмка Ozon"])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


def get_step_keyboard():
    return ReplyKeyboardMarkup([["🔙 Назад", "❌ Главное меню"]], resize_keyboard=True)


# --- START ENGINE ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    db: SupabaseService = context.bot_data.get("db")
    if db.is_approved(uid):
        await update.message.reply_text(f"👋 Добро пожаловать! Вы в системе.\n\n{RULES_TEXT}", reply_markup=get_main_menu_keyboard(uid), parse_mode="Markdown")
        return

    btn = [[KeyboardButton("📱 Поделиться контактом", request_contact=True)]]
    await update.message.reply_text("🔒 Доступ ограничен. Поделитесь контактом для авторизации:", reply_markup=ReplyKeyboardMarkup(btn, resize_keyboard=True))


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    c = update.message.contact
    uid = update.effective_user.id
    name = f"{update.effective_user.first_name or ''} {update.effective_user.last_name or ''}".strip()
    context.bot_data.get("db").add_pending(uid, c.phone_number, name)
    await update.message.reply_text("⏳ Заявка отправлена администратору.")

    kb = [[InlineKeyboardButton("✅ Допустить", callback_data=f"approve_{uid}_{c.phone_number}"),
           InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{uid}")]]
    await context.bot.send_message(chat_id=ADMIN_ID, text=f"🔔 Заявка:\n👤 {name}\n📞 {c.phone_number}", reply_markup=InlineKeyboardMarkup(kb))


async def approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("_")
    action, target_id = data[0], int(data[1])
    if action == "approve":
        phone = data[2] if len(data) > 2 else ""
        context.bot_data.get("db").approve_user(target_id, "", phone)
        await query.edit_message_text("✅ Пользователь допущен.")
        await context.bot.send_message(chat_id=target_id, text="🎉 Доступ одобрен! Введите /start")


async def cancel_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Возврат в главное меню.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


# --- SUPPLY PROCESS (одна заявка — несколько позиций) ---
async def show_supply_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [TYPE_BUTTONS_SUPPLY, ["❌ Главное меню"]]
    await update.message.reply_text("📦 *Новая закупка*\n\nШаг 1: Что закупаем?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")


async def show_supply_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.bot_data.get("db")
    sups = [s["name"] for s in db.get_suppliers_list(type_filter=context.user_data["s_cp_type"])]
    await update.message.reply_text("Шаг 2: Выберите поставщика:", reply_markup=build_grid_keyboard(sups, columns=2))


async def show_supply_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.bot_data.get("db")
    if context.user_data["s_cp_type"] == "поставщик сырья":
        items = db.get_all_product_names()
    else:
        items = db.get_consumables_list()
    n = len(context.user_data.get("s_items", []))
    label = f"Позиция №{n + 1}: выберите наименование:" if n else "Шаг 3: Выберите наименование:"
    await update.message.reply_text(label, reply_markup=build_grid_keyboard(items, columns=2))


async def show_supply_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"«{context.user_data['s_cur_product']}» — введите количество (число):", reply_markup=get_step_keyboard())


async def show_supply_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"«{context.user_data['s_cur_product']}» — введите цену за единицу:", reply_markup=get_step_keyboard())


def cart_summary_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    items = context.user_data.get("s_items", [])
    unit = context.user_data.get("s_unit", "")
    lines = []
    grand_total = 0.0
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {it['product']} — {it['qty']} {unit} × {it['price']} ₽ = {it['total']} ₽")
        grand_total += it["total"]
    return "\n".join(lines), round(grand_total, 2)


async def show_supply_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items_text, grand_total = cart_summary_text(context)
    kb = [["➕ Добавить ещё", "✅ Это всё"], ["🔙 Назад", "❌ Главное меню"]]
    await update.message.reply_text(
        f"🧺 *Текущая заявка:*\n{items_text}\n\n💰 Итого: *{grand_total} ₽*\n\nДобавить ещё позицию или завершить заявку?",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown"
    )


async def show_supply_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Добавьте общий комментарий к заявке (или нажмите '-'):", reply_markup=get_step_keyboard())


async def supply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["s_items"] = []
    await show_supply_category(update, context)
    return SUPPLY_CATEGORY


async def supply_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t in ("❌ Главное меню", "🔙 Назад"): return await cancel_to_menu(update, context)
    if t not in TYPE_BUTTON_TO_DB: return SUPPLY_CATEGORY
    context.user_data["s_type_label"] = t
    context.user_data["s_cp_type"] = TYPE_BUTTON_TO_DB[t]

    await show_supply_supplier(update, context)
    return SUPPLY_SUPPLIER


async def supply_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_supply_category(update, context)
        return SUPPLY_CATEGORY
    context.user_data["s_supplier"] = t
    context.user_data["s_items"] = []

    db = context.bot_data.get("db")
    cp = db.get_counterparty_by_name(t)
    context.user_data["s_phone"] = (cp or {}).get("phone", "")

    # И сырьё, и расходники приходят общим пулом на склад — ИП не спрашиваем,
    # распределение по конкретному ИП решается позже, на Фасовке.
    context.user_data["s_my_ip"] = WAREHOUSE_IP_LABEL
    context.user_data["s_unit"] = "кг" if context.user_data["s_cp_type"] == "поставщик сырья" else "шт"

    await show_supply_product(update, context)
    return SUPPLY_PRODUCT


async def supply_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        if context.user_data.get("s_items"):
            await show_supply_add_more(update, context)
            return SUPPLY_ADD_MORE
        await show_supply_supplier(update, context)
        return SUPPLY_SUPPLIER
    context.user_data["s_cur_product"] = t
    await show_supply_qty(update, context)
    return SUPPLY_QTY


async def supply_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_supply_product(update, context)
        return SUPPLY_PRODUCT
    try: context.user_data["s_cur_qty"] = float(t.replace(",", ".").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("⚠️ Нужно ввести число, например 500. Попробуйте ещё раз:", reply_markup=get_step_keyboard())
        return SUPPLY_QTY
    await show_supply_price(update, context)
    return SUPPLY_PRICE


async def supply_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_supply_qty(update, context)
        return SUPPLY_QTY
    try: price = float(t.replace(",", ".").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("⚠️ Нужно ввести число, например 260. Попробуйте ещё раз:", reply_markup=get_step_keyboard())
        return SUPPLY_PRICE

    qty = context.user_data["s_cur_qty"]
    context.user_data.setdefault("s_items", []).append({
        "product": context.user_data["s_cur_product"],
        "qty": qty,
        "price": price,
        "total": round(qty * price, 2),
    })
    await show_supply_add_more(update, context)
    return SUPPLY_ADD_MORE


async def supply_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        # Откатываем последнюю добавленную позицию, чтобы её можно было ввести заново
        items = context.user_data.get("s_items", [])
        if items:
            items.pop()
        await show_supply_qty(update, context)
        return SUPPLY_QTY
    if t == "➕ Добавить ещё":
        await show_supply_product(update, context)
        return SUPPLY_PRODUCT
    if t == "✅ Это всё":
        await show_supply_comment(update, context)
        return SUPPLY_COMMENT
    return SUPPLY_ADD_MORE


async def supply_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_supply_add_more(update, context)
        return SUPPLY_ADD_MORE
    context.user_data["s_comment"] = t if t != "-" else ""
    d = context.user_data
    items_text, grand_total = cart_summary_text(context)
    summary = f"📋 *Проверка заявки:*\n🏢 Поставщик: {d['s_supplier']}\n\n{items_text}\n\n💰 *Итого: {grand_total} ₽*"
    await update.message.reply_text(summary, reply_markup=ReplyKeyboardMarkup([["✅ Подтвердить"], ["🔙 Назад", "❌ Главное меню"]], resize_keyboard=True), parse_mode="Markdown")
    return SUPPLY_CONFIRM


async def supply_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "🔙 Назад":
        await show_supply_comment(update, context)
        return SUPPLY_COMMENT
    if t != "✅ Подтвердить": return await cancel_to_menu(update, context)
    db, d = context.bot_data.get("db"), context.user_data

    cp = db.get_counterparty_by_name(d["s_supplier"])
    category_name = infer_category_name(d.get("s_cp_type", ""))
    category_id = db.get_category_id(category_name)
    cp_id = (cp or {}).get("id")
    ip_id = db.get_ip_id(d["s_my_ip"])
    op_date = datetime.now(TZ_MSK).date().isoformat()
    is_raw = d.get("s_cp_type") == "поставщик сырья"
    flow_type = "сырьё_от_поставщика" if is_raw else "расходники_от_поставщика"

    for item in d.get("s_items", []):
        op_id = db.add_operation_returning_id(
            operation_date=op_date,
            ip_id=ip_id,
            counterparty_id=cp_id,
            category_id=category_id,
            operation_type="покупка",
            amount=item["total"],
            quantity=item["qty"],
            price=item["price"],
            item_name=item["product"],
            entered_by=str(update.effective_user.id),
            status="confirmed",
            payment_method=None,
            comment=d["s_comment"],
        )
        db.add_warehouse_movement(
            direction="приход",
            flow_type=flow_type,
            product_name=item["product"],
            quantity=item["qty"],
            unit=d.get("s_unit", "шт"),
            unit_price=item["price"],
            movement_date=op_date,
            counterparty_id=cp_id,
            ip_id=None,
            marketplace=None,
            note=d["s_comment"],
        )
        # Двойная запись: Дт Запасы на складе / Кт Расчёты с поставщиками
        db.post_journal_entry(
            operation_id=op_id, entry_date=op_date,
            debit_code="1200", credit_code="2000",
            amount=item["total"], comment=f"Закупка: {item['product']} у {d['s_supplier']}",
        )

    _, grand_total = cart_summary_text(context)
    n = len(d.get("s_items", []))
    await update.message.reply_text(
        f"✅ Заявка занесена в базу: {n} поз. на общую сумму {grand_total} ₽.\n📦 Остаток на складе обновлён автоматически.",
        reply_markup=get_main_menu_keyboard(update.effective_user.id)
    )
    return ConversationHandler.END


# --- PAYMENT PROCESS ---
async def show_payment_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [TYPE_BUTTONS_FULL, ["❌ Главное меню"]]
    await update.message.reply_text("💰 *Внесение оплаты*\n\nШаг 1: Кому платим?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")


async def show_payment_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.bot_data.get("db")
    sups = [s["name"] for s in db.get_suppliers_list(type_filter=context.user_data["p_cp_type"])]
    await update.message.reply_text("Шаг 2: Выберите получателя:", reply_markup=build_grid_keyboard(sups, columns=2))


async def show_payment_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.bot_data.get("db")
    debt = db.get_supplier_current_debt(context.user_data["p_supplier"])
    kb = [[f"{debt}"], ["Ввести сумму вручную"], ["🔙 Назад", "❌ Главное меню"]]
    await update.message.reply_text(f"Шаг 3: Текущий долг перед получателем: *{debt} ₽*.\nВыберите готовую сумму долга или нажмите ввод вручную:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")


async def show_payment_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Шаг 4: Выберите тип счёта оплаты:", reply_markup=ReplyKeyboardMarkup([["Наличные", "Карта ВТБ"], ["Безнал ИП"], ["🔙 Назад", "❌ Главное меню"]], resize_keyboard=True))


async def show_payment_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Добавьте комментарий (или '-'):", reply_markup=get_step_keyboard())


async def payment_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_payment_category(update, context)
    return PAYMENT_CATEGORY


async def payment_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t in ("❌ Главное меню", "🔙 Назад"): return await cancel_to_menu(update, context)
    if t not in TYPE_BUTTON_TO_DB: return PAYMENT_CATEGORY
    context.user_data["p_cp_type"] = TYPE_BUTTON_TO_DB[t]

    await show_payment_supplier(update, context)
    return PAYMENT_SUPPLIER


async def payment_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_payment_category(update, context)
        return PAYMENT_CATEGORY
    context.user_data["p_supplier"] = t

    db = context.bot_data.get("db")
    cp = db.get_counterparty_by_name(t)
    context.user_data["p_phone"] = (cp or {}).get("phone", "")

    await show_payment_amount(update, context)
    return PAYMENT_AMOUNT


async def payment_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_payment_supplier(update, context)
        return PAYMENT_SUPPLIER
    if t == "Ввести сумму вручную":
        await update.message.reply_text("Введите точную сумму платежа цифрами:", reply_markup=get_step_keyboard())
        return PAYMENT_AMOUNT

    try: context.user_data["p_amount"] = float(t.replace(",", ".").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("⚠️ Нужно ввести число, например 5000. Попробуйте ещё раз:", reply_markup=get_step_keyboard())
        return PAYMENT_AMOUNT

    await show_payment_type(update, context)
    return PAYMENT_TYPE


async def payment_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_payment_amount(update, context)
        return PAYMENT_AMOUNT
    context.user_data["p_type"] = t
    await show_payment_comment(update, context)
    return PAYMENT_COMMENT


async def payment_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_payment_type(update, context)
        return PAYMENT_TYPE
    context.user_data["p_comment"] = t if t != "-" else ""
    d = context.user_data
    await update.message.reply_text(f"💰 *Платёж:*\n🏢 Получатель: {d['p_supplier']}\n💰 Сумма: {d['p_amount']} ₽\n🏦 Счёт: {d['p_type']}", reply_markup=ReplyKeyboardMarkup([["✅ Подтвердить"], ["🔙 Назад", "❌ Главное меню"]], resize_keyboard=True), parse_mode="Markdown")
    return PAYMENT_CONFIRM


async def payment_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "🔙 Назад":
        await show_payment_comment(update, context)
        return PAYMENT_COMMENT
    if t != "✅ Подтвердить": return await cancel_to_menu(update, context)
    db, d = context.bot_data.get("db"), context.user_data

    cp = db.get_counterparty_by_name(d["p_supplier"])
    category_name = infer_category_name(d.get("p_cp_type", ""))
    op_date = datetime.now(TZ_MSK).date().isoformat()

    op_id = db.add_operation_returning_id(
        operation_date=op_date,
        ip_id=None,
        counterparty_id=(cp or {}).get("id"),
        category_id=db.get_category_id(category_name),
        operation_type="оплата",
        amount=-d["p_amount"],
        entered_by=str(update.effective_user.id),
        status="confirmed",
        payment_method=d["p_type"],
        comment=d["p_comment"],
    )
    # Двойная запись: Дт Расчёты с поставщиками/персоналом / Кт Касса или Расчётный счёт
    payables_code = "2100" if d.get("p_cp_type") == "сотрудник" else "2000"
    cash_code = "1000" if d["p_type"] == "Наличные" else "1010"
    db.post_journal_entry(
        operation_id=op_id, entry_date=op_date,
        debit_code=payables_code, credit_code=cash_code,
        amount=d["p_amount"], comment=f"Оплата: {d['p_supplier']} ({d['p_type']})",
    )
    await update.message.reply_text("✅ Оплата успешно сохранена в систему!", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


# --- BALANCE LOGIC ---
async def balance_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["🔍 Один контрагент", "📋 Все долги"], ["📗 Пробный баланс"], ["❌ Главное меню"]]
    await update.message.reply_text("📊 *Баланс*\n\nЧто показать?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")
    return BALANCE_MODE


async def balance_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t in ("❌ Главное меню", "🔙 Назад"): return await cancel_to_menu(update, context)
    db = context.bot_data.get("db")

    if t == "🔍 Один контрагент":
        sups = [s["name"] for s in db.get_suppliers_list()]
        await update.message.reply_text("Выберите контрагента для вывода точного баланса:", reply_markup=build_grid_keyboard(sups, columns=2))
        return BALANCE_SUPPLIER

    elif t == "📋 Все долги":
        debts = db.get_all_debts()
        if not debts:
            await update.message.reply_text("Долгов нет — все балансы на нуле.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
            return ConversationHandler.END

        type_icons = {"поставщик сырья": "🌰", "поставщик расходников": "📦", "сотрудник": "👤"}
        lines = ["📋 *Все текущие долги:*\n"]
        grand_total = 0.0
        for d in debts:
            icon = type_icons.get(d["type"], "▫️")
            lines.append(f"{icon} {d['name']}: *{d['balance']} ₽*")
            grand_total += d["balance"]
        lines.append(f"\n💰 *Итого по всем контрагентам: {round(grand_total, 2)} ₽*")
        await update.message.reply_text("\n".join(lines), reply_markup=get_main_menu_keyboard(update.effective_user.id), parse_mode="Markdown")
        return ConversationHandler.END

    elif t == "📗 Пробный баланс":
        rows = db.get_trial_balance()
        if not rows:
            await update.message.reply_text("Проводок ещё нет.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
            return ConversationHandler.END
        lines = ["📗 *Пробный баланс*\n_Счёт — Дебет / Кредит_\n"]
        total_debit = total_credit = 0.0
        for r in rows:
            lines.append(f"{r['code']} {r['name']}: Дт {round(r['debit'],2)} / Кт {round(r['credit'],2)}")
            total_debit += r["debit"]
            total_credit += r["credit"]
        lines.append(f"\n*Итого:* Дт {round(total_debit,2)} / Кт {round(total_credit,2)}")
        status = "✅ Баланс сходится" if abs(total_debit - total_credit) < 0.01 else "⚠️ Расхождение!"
        lines.append(status)
        await update.message.reply_text("\n".join(lines), reply_markup=get_main_menu_keyboard(update.effective_user.id), parse_mode="Markdown")
        return ConversationHandler.END

    return BALANCE_MODE


async def balance_calculate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад": return await balance_start(update, context)
    debt = context.bot_data.get("db").get_supplier_current_debt(t)
    await update.message.reply_text(f"📊 Контрагент: `{t}`\n💰 Текущий баланс долга: *{debt}* ₽", reply_markup=get_main_menu_keyboard(update.effective_user.id), parse_mode="Markdown")
    return ConversationHandler.END


# --- HISTORY BY SUPPLIER ---
async def show_history_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [TYPE_BUTTONS_FULL, ["❌ Главное меню"]]
    await update.message.reply_text("📜 *История по контрагенту*\n\nШаг 1: Какой тип?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")


async def history_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_history_category(update, context)
    return HISTORY_CATEGORY


async def history_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t in ("❌ Главное меню", "🔙 Назад"): return await cancel_to_menu(update, context)
    if t not in TYPE_BUTTON_TO_DB: return HISTORY_CATEGORY
    context.user_data["h_type"] = TYPE_BUTTON_TO_DB[t]
    db = context.bot_data.get("db")
    sups = [s["name"] for s in db.get_suppliers_list(type_filter=TYPE_BUTTON_TO_DB[t])]
    await update.message.reply_text("Шаг 2: Выберите контрагента:", reply_markup=build_grid_keyboard(sups, columns=2))
    return HISTORY_SUPPLIER


async def history_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_history_category(update, context)
        return HISTORY_CATEGORY
    db = context.bot_data.get("db")
    cp = db.get_counterparty_by_name(t)
    if not cp:
        await update.message.reply_text("Контрагент не найден.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
        return ConversationHandler.END

    rows = db.get_history(cp["id"])
    if not rows:
        await update.message.reply_text(f"📜 История по «{t}» пуста.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
        return ConversationHandler.END

    context.user_data["history_rows"] = rows
    context.user_data["history_name"] = t

    running = 0.0
    lines = [f"📜 *История: {t}*\n"]
    for i, r in enumerate(rows, 1):
        running += float(r["amount"])
        sign = "➕" if float(r["amount"]) >= 0 else "➖"
        item = r.get("item_name") or ""
        reversed_tag = " ↩️сторно" if r.get("reversed_by") else ""
        lines.append(f"{i}. {sign} {r.get('operation_date','')} | {r.get('operation_type','')} {item} | {r['amount']} ₽ | итог: {round(running,2)} ₽{reversed_tag}")

    text = "\n".join([lines[0]] + lines[-20:]) if len(lines) > 21 else "\n".join(lines)
    text += f"\n\n💰 *Текущий остаток долга: {round(running,2)} ₽*"
    kb = [["↩️ Отменить операцию"], ["❌ Главное меню"]]
    await update.message.reply_text(text, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")
    return HISTORY_REVERSE_SELECT


async def history_reverse_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t != "↩️ Отменить операцию":
        return await cancel_to_menu(update, context)
    await update.message.reply_text(
        "Введите номер операции для отмены (число из списка выше):",
        reply_markup=ReplyKeyboardMarkup([["❌ Главное меню"]], resize_keyboard=True)
    )
    return HISTORY_REVERSE_NUMBER


async def history_reverse_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    rows = context.user_data.get("history_rows", [])
    try:
        idx = int(t) - 1
        if idx < 0 or idx >= len(rows):
            raise ValueError
    except ValueError:
        await update.message.reply_text(f"⚠️ Введите число от 1 до {len(rows)}:")
        return HISTORY_REVERSE_NUMBER

    row = rows[idx]
    if row.get("reversed_by"):
        await update.message.reply_text("⚠️ Эта операция уже отменена ранее.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
        return ConversationHandler.END

    context.user_data["reverse_target"] = row
    item = row.get("item_name") or ""
    summary = (
        f"↩️ *Подтвердите отмену операции:*\n"
        f"{row.get('operation_date','')} | {row.get('operation_type','')} {item} | {row['amount']} ₽\n\n"
        f"Будет создана компенсирующая запись на {-float(row['amount'])} ₽."
    )
    await update.message.reply_text(summary, reply_markup=ReplyKeyboardMarkup([["✅ Подтвердить отмену"], ["❌ Главное меню"]], resize_keyboard=True), parse_mode="Markdown")
    return HISTORY_REVERSE_CONFIRM


async def history_reverse_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t != "✅ Подтвердить отмену": return await cancel_to_menu(update, context)
    db = context.bot_data.get("db")
    row = context.user_data["reverse_target"]
    op_date = datetime.now(TZ_MSK).date().isoformat()

    reversal_id = db.add_operation_returning_id(
        operation_date=op_date,
        ip_id=row.get("ip_id"),
        counterparty_id=row.get("counterparty_id"),
        category_id=row.get("category_id"),
        operation_type="сторно",
        amount=-float(row["amount"]),
        quantity=row.get("quantity"),
        price=row.get("price"),
        item_name=row.get("item_name"),
        entered_by=str(update.effective_user.id),
        status="confirmed",
        payment_method=row.get("payment_method"),
        comment=f"Сторно операции №{row['id']}",
        reversal_of=row["id"],
    )
    db.mark_operation_reversed(row["id"], reversal_id)

    # Зеркалим проводку исходной операции (дебет и кредит меняются местами)
    orig_entry = db.get_journal_entry_for_operation(row["id"])
    if orig_entry:
        debit_code = db.get_account_code_by_id(orig_entry["credit_account_id"])
        credit_code = db.get_account_code_by_id(orig_entry["debit_account_id"])
        if debit_code and credit_code:
            db.post_journal_entry(
                operation_id=reversal_id, entry_date=op_date,
                debit_code=debit_code, credit_code=credit_code,
                amount=float(orig_entry["amount"]), comment=f"Сторно проводки операции №{row['id']}",
            )

    await update.message.reply_text(
        f"✅ Операция отменена. Создана компенсирующая запись на {-float(row['amount'])} ₽.\n"
        f"Исходная запись осталась в истории (не удалена).",
        reply_markup=get_main_menu_keyboard(update.effective_user.id)
    )
    return ConversationHandler.END


# --- WAREHOUSE (СКЛАД) ---
async def warehouse_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["📤 Фасовка/Отгрузка", "📊 Остаток на складе"], ["💸 Постоянные расходы", "🚚 Логистика до МП"], ["👤 Сотрудники", "📋 История работ"], ["❌ Главное меню"]]
    await update.message.reply_text("🏭 *Склад*\n\nЧто делаем?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")
    return WAREHOUSE_MENU


async def warehouse_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t in ("❌ Главное меню", "🔙 Назад"): return await cancel_to_menu(update, context)
    db = context.bot_data.get("db")

    if t == "📤 Фасовка/Отгрузка":
        ips = db.get_my_ip_list()
        await update.message.reply_text("Шаг 1: На какое ИП фасуем и отгружаем?", reply_markup=build_grid_keyboard(ips, columns=2))
        return WAREHOUSE_OUT_IP

    elif t == "📊 Остаток на складе":
        balances = db.get_stock_balances()
        if not balances:
            await update.message.reply_text("Склад пуст — движений ещё не было.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
            return ConversationHandler.END
        lines = ["📊 *Остаток на складе:*\n"]
        for name, qty in balances:
            lines.append(f"▫️ {name}: *{round(qty,2)}* кг/шт")
        await update.message.reply_text("\n".join(lines), reply_markup=get_main_menu_keyboard(update.effective_user.id), parse_mode="Markdown")
        return ConversationHandler.END

    elif t == "💸 Постоянные расходы":
        kb2 = [
            ["Аренда склада", "Договор оказания услуг"],
            ["Пропуск", "Коммунальные услуги"],
            ["🔙 Назад", "❌ Главное меню"],
        ]
        await update.message.reply_text("Шаг 1: Категория расхода:", reply_markup=ReplyKeyboardMarkup(kb2, resize_keyboard=True))
        return WAREHOUSE_EXPENSE_CATEGORY

    elif t == "🚚 Логистика до МП":
        kb = [["Ozon", "WB"], ["Яндекс", "Общий (несколько площадок)"], ["🔙 Назад", "❌ Главное меню"]]
        await update.message.reply_text("🚚 *Логистика до маркетплейса*\n\nШаг 1: Куда везли (площадка)?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")
        return LOGISTICS_MARKETPLACE

    elif t == "👤 Сотрудники":
        return await employee_start(update, context)

    elif t == "📋 История работ":
        rows = db.get_warehouse_activity_history()
        if not rows:
            await update.message.reply_text("История пуста — записей ещё не было.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
            return ConversationHandler.END
        lines = ["📋 *История работ по складу*\n"]
        for r in rows[-25:]:
            lines.append(f"▫️ {r['date']} | {r['label']} | {r['detail']}")
        await update.message.reply_text("\n".join(lines), reply_markup=get_main_menu_keyboard(update.effective_user.id), parse_mode="Markdown")
        return ConversationHandler.END

    return WAREHOUSE_MENU


WAREHOUSE_EXPENSE_CATEGORY_MAP = {
    "Аренда склада": "Аренда склада",
    "Договор оказания услуг": "Договор оказания услуг",
    "Пропуск": "Пропуск",
    "Коммунальные услуги": "Коммунальные услуги",
}


async def warehouse_expense_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад": return await warehouse_start(update, context)
    if t not in WAREHOUSE_EXPENSE_CATEGORY_MAP: return WAREHOUSE_EXPENSE_CATEGORY
    context.user_data["wexp_category_label"] = t
    context.user_data["wexp_category_name"] = WAREHOUSE_EXPENSE_CATEGORY_MAP[t]
    kb = [["Наличные", "Карта ВТБ"], ["Безнал ИП"], ["🔙 Назад", "❌ Главное меню"]]
    await update.message.reply_text("Шаг 2: Способ оплаты:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return WAREHOUSE_EXPENSE_PAYMENT


async def warehouse_expense_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        kb2 = [
            ["Аренда склада", "Договор оказания услуг"],
            ["Пропуск", "Коммунальные услуги"],
            ["🔙 Назад", "❌ Главное меню"],
        ]
        await update.message.reply_text("Шаг 1: Категория расхода:", reply_markup=ReplyKeyboardMarkup(kb2, resize_keyboard=True))
        return WAREHOUSE_EXPENSE_CATEGORY
    context.user_data["wexp_payment"] = t
    await update.message.reply_text("Шаг 3: Сумма (₽):", reply_markup=get_step_keyboard())
    return WAREHOUSE_EXPENSE_AMOUNT


async def warehouse_expense_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        kb = [["Наличные", "Карта ВТБ"], ["Безнал ИП"], ["🔙 Назад", "❌ Главное меню"]]
        await update.message.reply_text("Шаг 2: Способ оплаты:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return WAREHOUSE_EXPENSE_PAYMENT
    try: context.user_data["wexp_amount"] = float(t.replace(",", ".").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("⚠️ Нужно ввести число. Попробуйте ещё раз:", reply_markup=get_step_keyboard())
        return WAREHOUSE_EXPENSE_AMOUNT
    await update.message.reply_text("Комментарий (или '-'):", reply_markup=get_step_keyboard())
    return WAREHOUSE_EXPENSE_COMMENT


async def warehouse_expense_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await update.message.reply_text("Шаг 3: Сумма (₽):", reply_markup=get_step_keyboard())
        return WAREHOUSE_EXPENSE_AMOUNT
    context.user_data["wexp_comment"] = t if t != "-" else ""
    d = context.user_data
    summary = f"💸 *Постоянный расход:*\n📂 Категория: {d['wexp_category_label']}\n💳 Оплата: {d['wexp_payment']}\n💰 Сумма: {d['wexp_amount']} ₽"
    await update.message.reply_text(summary, reply_markup=ReplyKeyboardMarkup([["✅ Подтвердить"], ["🔙 Назад", "❌ Главное меню"]], resize_keyboard=True), parse_mode="Markdown")
    return WAREHOUSE_EXPENSE_CONFIRM


async def warehouse_expense_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "🔙 Назад":
        await warehouse_expense_comment_back(update, context)
        return WAREHOUSE_EXPENSE_COMMENT
    if t != "✅ Подтвердить": return await cancel_to_menu(update, context)
    db, d = context.bot_data.get("db"), context.user_data
    op_date = datetime.now(TZ_MSK).date().isoformat()

    category_id = db.get_category_id(d["wexp_category_name"])
    op_id = db.add_operation_returning_id(
        operation_date=op_date,
        ip_id=None,
        counterparty_id=None,
        category_id=category_id,
        operation_type="расход",
        amount=d["wexp_amount"],
        entered_by=str(update.effective_user.id),
        status="confirmed",
        payment_method=d["wexp_payment"],
        comment=d["wexp_comment"],
    )
    cash_code = "1000" if d["wexp_payment"] == "Наличные" else "1010"
    db.post_journal_entry(
        operation_id=op_id, entry_date=op_date,
        debit_code="5900", credit_code=cash_code,
        amount=d["wexp_amount"], comment=f"{d['wexp_category_label']}: {d['wexp_comment']}",
    )
    await update.message.reply_text("✅ Расход внесён.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


async def warehouse_expense_comment_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Комментарий (или '-'):", reply_markup=get_step_keyboard())


# --- Логистика до маркетплейса (реальные данные, не оценка) ---
async def logistics_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t in ("❌ Главное меню", "🔙 Назад"): return await cancel_to_menu(update, context)
    db = context.bot_data.get("db")

    if t == "➕ Внести":
        kb = [["Ozon", "WB"], ["Яндекс", "Общий (несколько площадок)"], ["🔙 Назад", "❌ Главное меню"]]
        await update.message.reply_text("Шаг 1: Куда везли (площадка)?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return LOGISTICS_MARKETPLACE

    elif t == "📜 История":
        rows = db.get_logistics_history()
        if not rows:
            await update.message.reply_text("История пуста — записей ещё не было.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
            return ConversationHandler.END
        total_amount = sum(float(r["amount"]) for r in rows)
        total_qty = sum(float(r["quantity"] or 0) for r in rows)
        lines = ["🚚 *История логистики до МП*\n"]
        for r in rows[-20:]:
            qty = r.get("quantity")
            per_kg = f" ({round(float(r['amount'])/float(qty),2)}₽/шт)" if qty else ""
            lines.append(f"▫️ {r.get('operation_date','')} | {r.get('comment','')} | {r['amount']}\u20bd{per_kg}")
        avg = round(total_amount / total_qty, 2) if total_qty else None
        lines.append(f"\n💰 Всего потрачено: {round(total_amount,2)} ₽")
        if avg:
            lines.append(f"⚖️ Средняя стоимость: {avg} ₽/шт")
        await update.message.reply_text("\n".join(lines), reply_markup=get_main_menu_keyboard(update.effective_user.id), parse_mode="Markdown")
        return ConversationHandler.END

    return LOGISTICS_MENU


async def logistics_marketplace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад": return await warehouse_start(update, context)
    context.user_data["log_marketplace"] = t
    await update.message.reply_text("Шаг 2: Сколько ШТУК товара было в этой перевозке (на палете, суммарно)?", reply_markup=get_step_keyboard())
    return LOGISTICS_QTY


async def logistics_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        kb = [["Ozon", "WB"], ["Яндекс", "Общий (несколько площадок)"], ["🔙 Назад", "❌ Главное меню"]]
        await update.message.reply_text("Шаг 1: Куда везли (площадка)?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return LOGISTICS_MARKETPLACE
    try: context.user_data["log_qty"] = float(t.replace(",", ".").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("⚠️ Нужно ввести число. Попробуйте ещё раз:", reply_markup=get_step_keyboard())
        return LOGISTICS_QTY
    await update.message.reply_text("Шаг 3: Сумма за перевозку (₽):", reply_markup=get_step_keyboard())
    return LOGISTICS_AMOUNT


async def logistics_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await update.message.reply_text("Шаг 2: Сколько ШТУК товара было в этой перевозке (на палете, суммарно)?", reply_markup=get_step_keyboard())
        return LOGISTICS_QTY
    try: context.user_data["log_amount"] = float(t.replace(",", ".").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("⚠️ Нужно ввести число. Попробуйте ещё раз:", reply_markup=get_step_keyboard())
        return LOGISTICS_AMOUNT
    kb = [["Наличные", "Карта ВТБ"], ["Безнал ИП"], ["🔙 Назад", "❌ Главное меню"]]
    await update.message.reply_text("Шаг 4: Способ оплаты:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return LOGISTICS_PAYMENT


async def logistics_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await update.message.reply_text("Шаг 3: Сумма за перевозку (₽):", reply_markup=get_step_keyboard())
        return LOGISTICS_AMOUNT
    context.user_data["log_payment"] = t
    await update.message.reply_text("Комментарий (например, номер поставки) или '-':", reply_markup=get_step_keyboard())
    return LOGISTICS_COMMENT


async def logistics_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        kb = [["Наличные", "Карта ВТБ"], ["Безнал ИП"], ["🔙 Назад", "❌ Главное меню"]]
        await update.message.reply_text("Шаг 4: Способ оплаты:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return LOGISTICS_PAYMENT
    context.user_data["log_comment"] = t if t != "-" else ""
    d = context.user_data
    per_kg = round(d["log_amount"] / d["log_qty"], 2) if d["log_qty"] else 0
    summary = f"🚚 *Логистика до {d['log_marketplace']}:*\n⚖️ {d['log_qty']} шт\n💰 {d['log_amount']} ₽ ({per_kg} ₽/шт)"
    await update.message.reply_text(summary, reply_markup=ReplyKeyboardMarkup([["✅ Подтвердить"], ["🔙 Назад", "❌ Главное меню"]], resize_keyboard=True), parse_mode="Markdown")
    return LOGISTICS_CONFIRM


async def logistics_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "🔙 Назад":
        await update.message.reply_text("Комментарий (например, номер поставки) или '-':", reply_markup=get_step_keyboard())
        return LOGISTICS_COMMENT
    if t != "✅ Подтвердить": return await cancel_to_menu(update, context)
    db, d = context.bot_data.get("db"), context.user_data
    op_date = datetime.now(TZ_MSK).date().isoformat()

    category_id = db.get_category_id("Логистика до маркетплейса")
    op_id = db.add_operation_returning_id(
        operation_date=op_date,
        ip_id=None,
        counterparty_id=None,
        category_id=category_id,
        operation_type="расход",
        amount=d["log_amount"],
        quantity=d["log_qty"],
        price=round(d["log_amount"] / d["log_qty"], 2) if d["log_qty"] else None,
        entered_by=str(update.effective_user.id),
        status="confirmed",
        payment_method=d["log_payment"],
        comment=f"{d['log_marketplace']}: {d['log_comment']}",
    )
    cash_code = "1000" if d["log_payment"] == "Наличные" else "1010"
    db.post_journal_entry(
        operation_id=op_id, entry_date=op_date,
        debit_code="5900", credit_code=cash_code,
        amount=d["log_amount"], comment=f"Логистика до {d['log_marketplace']}: {d['log_comment']}",
    )
    await update.message.reply_text("✅ Логистика внесена.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


# --- Приход сырья ---
async def show_warehouse_in_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.bot_data.get("db")
    sups = [s["name"] for s in db.get_suppliers_list(type_filter="поставщик сырья")]
    await update.message.reply_text("Шаг 1: От какого поставщика пришёл товар?", reply_markup=build_grid_keyboard(sups, columns=2))


async def show_warehouse_in_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.bot_data.get("db")
    products = db.get_all_product_names()
    await update.message.reply_text("Шаг 2: Какой товар пришёл?", reply_markup=build_grid_keyboard(products, columns=2))


async def show_warehouse_in_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Шаг 3: Сколько кг пришло?", reply_markup=get_step_keyboard())


async def show_warehouse_in_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Комментарий (или '-'):", reply_markup=get_step_keyboard())


async def warehouse_in_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад": return await warehouse_start(update, context)
    context.user_data["w_supplier"] = t
    await show_warehouse_in_product(update, context)
    return WAREHOUSE_IN_PRODUCT


async def warehouse_in_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_warehouse_in_supplier(update, context)
        return WAREHOUSE_IN_SUPPLIER
    context.user_data["w_product"] = t
    await show_warehouse_in_qty(update, context)
    return WAREHOUSE_IN_QTY


async def warehouse_in_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_warehouse_in_product(update, context)
        return WAREHOUSE_IN_PRODUCT
    try: context.user_data["w_qty"] = float(t.replace(",", ".").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("⚠️ Нужно ввести число, например 500. Попробуйте ещё раз:", reply_markup=get_step_keyboard())
        return WAREHOUSE_IN_QTY
    await show_warehouse_in_comment(update, context)
    return WAREHOUSE_IN_COMMENT


async def warehouse_in_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_warehouse_in_qty(update, context)
        return WAREHOUSE_IN_QTY
    context.user_data["w_comment"] = t if t != "-" else ""
    d = context.user_data
    summary = f"📥 *Приход на склад:*\n🏢 Поставщик: {d['w_supplier']}\n📦 Товар: {d['w_product']}\n⚖️ Вес: {d['w_qty']} кг"
    await update.message.reply_text(summary, reply_markup=ReplyKeyboardMarkup([["✅ Подтвердить"], ["🔙 Назад", "❌ Главное меню"]], resize_keyboard=True), parse_mode="Markdown")
    return WAREHOUSE_IN_CONFIRM


async def warehouse_in_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "🔙 Назад":
        await show_warehouse_in_comment(update, context)
        return WAREHOUSE_IN_COMMENT
    if t != "✅ Подтвердить": return await cancel_to_menu(update, context)
    db, d = context.bot_data.get("db"), context.user_data
    cp = db.get_counterparty_by_name(d["w_supplier"])
    db.add_warehouse_movement(
        direction="приход",
        flow_type="сырьё_от_поставщика",
        product_name=d["w_product"],
        quantity=d["w_qty"],
        unit="кг",
        movement_date=datetime.now(TZ_MSK).date().isoformat(),
        counterparty_id=(cp or {}).get("id"),
        ip_id=None,
        marketplace=None,
        note=d["w_comment"],
    )
    await update.message.reply_text("✅ Приход на склад зафиксирован!", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


def parse_packaging_grams(text: str):
    """Пытается вытащить вес одной упаковки в граммах из текста фасовки: '200г', '1кг', '0.5 кг' и т.п."""
    raw = text.strip().lower().replace(" ", "").replace(",", ".")
    m = re.match(r"^([\d.]+)(г|гр|грамм|кг|kg|g)?$", raw)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2) or "г"
    return value * 1000 if unit in ("кг", "kg") else value


# --- Фасовка / Отгрузка (одна отгрузка — несколько позиций) ---
async def show_warehouse_out_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.bot_data.get("db")
    ips = db.get_my_ip_list()
    await update.message.reply_text("Шаг 1: На какое ИП фасуем и отгружаем?", reply_markup=build_grid_keyboard(ips, columns=2))


async def show_warehouse_out_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.bot_data.get("db")
    products = db.get_all_product_names()
    n = len(context.user_data.get("w_items", []))
    label = f"Позиция №{n + 1}: какой товар фасуем?" if n else "Шаг 2: Какой товар фасуем?"
    await update.message.reply_text(label, reply_markup=build_grid_keyboard(products, columns=2))


async def show_warehouse_out_packaging(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"«{context.user_data['w_cur_product']}» — фасовка (например «200г»):", reply_markup=get_step_keyboard())


async def show_warehouse_out_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    grams = parse_packaging_grams(context.user_data["w_cur_packaging"])
    context.user_data["w_cur_grams"] = grams
    if grams:
        await update.message.reply_text(
            f"«{context.user_data['w_cur_product']}» ({context.user_data['w_cur_packaging']}) — сколько штук (упаковок)?",
            reply_markup=get_step_keyboard()
        )
    else:
        # Не удалось распознать вес фасовки — просим вес напрямую, как раньше
        await update.message.reply_text(
            f"«{context.user_data['w_cur_product']}» ({context.user_data['w_cur_packaging']}) — не понял вес фасовки, введите общий вес в кг:",
            reply_markup=get_step_keyboard()
        )


def warehouse_cart_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    items = context.user_data.get("w_items", [])
    lines = []
    total_qty = 0.0
    for i, it in enumerate(items, 1):
        if it.get("pieces") is not None:
            lines.append(f"{i}. {it['product']} ({it['packaging']}) — {it['pieces']} уп. = {it['qty']} кг")
        else:
            lines.append(f"{i}. {it['product']} ({it['packaging']}) — {it['qty']} кг")
        total_qty += it["qty"]
    return "\n".join(lines), round(total_qty, 2)


async def show_warehouse_out_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items_text, total_qty = warehouse_cart_text(context)
    kb = [["➕ Добавить ещё", "✅ Это всё"], ["🔙 Назад", "❌ Главное меню"]]
    await update.message.reply_text(
        f"📦 *Текущая отгрузка:*\n{items_text}\n\n⚖️ Всего: *{total_qty} кг*\n\nДобавить ещё позицию или завершить?",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown"
    )


async def show_warehouse_out_marketplace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["Ozon", "WB"], ["Яндекс"], ["🔙 Назад", "❌ Главное меню"]]
    await update.message.reply_text("Куда отгружаем (общая площадка для всей партии)?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))


async def warehouse_out_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад": return await warehouse_start(update, context)
    context.user_data["w_ip"] = t
    context.user_data["w_items"] = []
    await show_warehouse_out_product(update, context)
    return WAREHOUSE_OUT_PRODUCT


async def warehouse_out_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        if context.user_data.get("w_items"):
            await show_warehouse_out_add_more(update, context)
            return WAREHOUSE_OUT_ADD_MORE
        await show_warehouse_out_ip(update, context)
        return WAREHOUSE_OUT_IP
    context.user_data["w_cur_product"] = t
    await show_warehouse_out_packaging(update, context)
    return WAREHOUSE_OUT_PACKAGING


async def warehouse_out_packaging(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_warehouse_out_product(update, context)
        return WAREHOUSE_OUT_PRODUCT
    context.user_data["w_cur_packaging"] = t
    await show_warehouse_out_qty(update, context)
    return WAREHOUSE_OUT_QTY


async def warehouse_out_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_warehouse_out_packaging(update, context)
        return WAREHOUSE_OUT_PACKAGING
    try: value = float(t.replace(",", ".").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("⚠️ Нужно ввести число. Попробуйте ещё раз:", reply_markup=get_step_keyboard())
        return WAREHOUSE_OUT_QTY

    grams = context.user_data.get("w_cur_grams")
    if grams:
        pieces = value
        qty_kg = round(grams * pieces / 1000, 3)
        item = {
            "product": context.user_data["w_cur_product"],
            "packaging": context.user_data["w_cur_packaging"],
            "pieces": pieces,
            "qty": qty_kg,
        }
    else:
        item = {
            "product": context.user_data["w_cur_product"],
            "packaging": context.user_data["w_cur_packaging"],
            "pieces": None,
            "qty": value,
        }

    context.user_data.setdefault("w_items", []).append(item)
    await show_warehouse_out_add_more(update, context)
    return WAREHOUSE_OUT_ADD_MORE


async def warehouse_out_add_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        items = context.user_data.get("w_items", [])
        if items:
            items.pop()
        await show_warehouse_out_qty(update, context)
        return WAREHOUSE_OUT_QTY
    if t == "➕ Добавить ещё":
        await show_warehouse_out_product(update, context)
        return WAREHOUSE_OUT_PRODUCT
    if t == "✅ Это всё":
        await show_warehouse_out_marketplace(update, context)
        return WAREHOUSE_OUT_MARKETPLACE
    return WAREHOUSE_OUT_ADD_MORE


async def warehouse_out_marketplace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_warehouse_out_add_more(update, context)
        return WAREHOUSE_OUT_ADD_MORE
    context.user_data["w_marketplace"] = t
    d = context.user_data
    items_text, total_qty = warehouse_cart_text(context)
    summary = f"📤 *Проверка отгрузки:*\n🏛 ИП: {d['w_ip']}\n\n{items_text}\n\n⚖️ Всего: *{total_qty} кг*\n🛒 Площадка: {t}"
    await update.message.reply_text(summary, reply_markup=ReplyKeyboardMarkup([["✅ Подтвердить"], ["🔙 Назад", "❌ Главное меню"]], resize_keyboard=True), parse_mode="Markdown")
    return WAREHOUSE_OUT_CONFIRM


async def warehouse_out_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "🔙 Назад":
        await show_warehouse_out_marketplace(update, context)
        return WAREHOUSE_OUT_MARKETPLACE
    if t != "✅ Подтвердить": return await cancel_to_menu(update, context)
    db, d = context.bot_data.get("db"), context.user_data

    ip_id = db.get_ip_id(d["w_ip"])
    move_date = datetime.now(TZ_MSK).date().isoformat()
    for item in d.get("w_items", []):
        db.add_warehouse_movement(
            direction="расход",
            flow_type="отгрузка_маркетплейс",
            product_name=item["product"],
            packaging=item["packaging"],
            quantity=item["qty"],
            unit="кг",
            movement_date=move_date,
            counterparty_id=None,
            ip_id=ip_id,
            marketplace=d["w_marketplace"],
            note="",
        )
        # Списание себестоимости сырья со склада — именно здесь товар физически уходит
        cost_per_kg = db.get_material_cost_per_kg(item["product"])
        if cost_per_kg and item["qty"]:
            cogs = round(cost_per_kg * item["qty"], 2)
            db.post_journal_entry(
                operation_id=None, entry_date=move_date,
                debit_code="5950", credit_code="1200",
                amount=cogs, comment=f"Себестоимость отгрузки: {item['product']} {item['qty']}кг",
            )
    n = len(d.get("w_items", []))
    _, total_qty = warehouse_cart_text(context)
    await update.message.reply_text(f"✅ Отгрузка зафиксирована: {n} поз., {total_qty} кг всего. Остаток на складе обновлён!", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


def parse_flexible_date(text: str, tz):
    """Принимает 'Сегодня', 'Вчера', '15', '15.07' или '15.07.2026'. Возвращает (iso_date, display) или None."""
    raw = text.strip().lower()
    now = datetime.now(tz)
    if raw == "сегодня":
        d = now.date()
        return d.isoformat(), d.strftime("%d.%m.%Y")
    if raw == "вчера":
        d = (now - timedelta(days=1)).date()
        return d.isoformat(), d.strftime("%d.%m.%Y")

    parts = text.strip().replace(" ", "").split(".")
    try:
        if len(parts) == 1 and parts[0].isdigit():
            d = now.date().replace(day=int(parts[0]))
        elif len(parts) == 2 and all(p.isdigit() for p in parts):
            d = now.date().replace(day=int(parts[0]), month=int(parts[1]))
        elif len(parts) == 3 and all(p.isdigit() for p in parts):
            year = int(parts[2])
            if year < 100: year += 2000
            d = now.date().replace(day=int(parts[0]), month=int(parts[1]), year=year)
        else:
            return None
    except ValueError:
        return None
    return d.isoformat(), d.strftime("%d.%m.%Y")


def parse_flexible_time(text: str):
    """Принимает '9', '09', '900', '0900', '09:00'. Возвращает 'ЧЧ:ММ' или None."""
    raw = text.strip().replace(" ", "")
    if ":" in raw:
        try:
            datetime.strptime(raw, "%H:%M")
            return raw
        except ValueError:
            return None
    if not raw.isdigit():
        return None
    if len(raw) <= 2:
        h, m = int(raw), 0
    elif len(raw) == 3:
        h, m = int(raw[0]), int(raw[1:3])
    elif len(raw) == 4:
        h, m = int(raw[0:2]), int(raw[2:4])
    else:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


# --- EMPLOYEES (СОТРУДНИКИ) ---
async def employee_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["🕐 Внести смену", "💵 Начислить оклад"], ["❌ Главное меню"]]
    await update.message.reply_text("👤 *Сотрудники*\n\nЧто делаем?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")
    return EMPLOYEE_MENU


async def employee_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t in ("❌ Главное меню", "🔙 Назад"): return await cancel_to_menu(update, context)
    db = context.bot_data.get("db")

    if t == "🕐 Внести смену":
        emps = [e["name"] for e in db.get_hourly_employees()]
        if not emps:
            await update.message.reply_text("Нет сотрудников с почасовой ставкой.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
            return ConversationHandler.END
        await update.message.reply_text("Шаг 1: Выберите сотрудника:", reply_markup=build_grid_keyboard(emps, columns=2))
        return EMP_SHIFT_EMPLOYEE

    elif t == "💵 Начислить оклад":
        emps = [e["name"] for e in db.get_all_employees()]
        await update.message.reply_text("Шаг 1: Выберите сотрудника:", reply_markup=build_grid_keyboard(emps, columns=2))
        return EMP_ACCRUAL_EMPLOYEE

    return EMPLOYEE_MENU


# --- Внести смену (почасовые) ---
async def show_emp_shift_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t_now = datetime.now(TZ_MSK)
    d0 = t_now.strftime("%d.%m")
    d1 = (t_now - timedelta(days=1)).strftime("%d.%m")
    kb = [[f"Сегодня ({d0})", f"Вчера ({d1})"], ["🔙 Назад", "❌ Главное меню"]]
    await update.message.reply_text(
        "Шаг 2: За какой день смена?\nМожно нажать кнопку, или просто написать число (напр. `15` или `15.07`):",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown"
    )


async def show_emp_shift_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Шаг 3: Время начала смены — просто цифрами, например `9` или `900` (=09:00):", reply_markup=get_step_keyboard(), parse_mode="Markdown")


async def show_emp_shift_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Шаг 4: Время окончания — например `21` или `2100` (=21:00):", reply_markup=get_step_keyboard(), parse_mode="Markdown")


async def emp_shift_employee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад": return await employee_start(update, context)
    db = context.bot_data.get("db")
    emp = db.get_counterparty_by_name(t)
    if not emp or emp.get("hourly_rate") is None:
        await update.message.reply_text("Сотрудник не найден или у него нет часовой ставки.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
        return ConversationHandler.END
    context.user_data["emp_name"] = t
    context.user_data["emp_id"] = emp["id"]
    context.user_data["emp_rate"] = float(emp["hourly_rate"])
    await show_emp_shift_date(update, context)
    return EMP_SHIFT_DATE


async def emp_shift_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        db = context.bot_data.get("db")
        emps = [e["name"] for e in db.get_hourly_employees()]
        await update.message.reply_text("Шаг 1: Выберите сотрудника:", reply_markup=build_grid_keyboard(emps, columns=2))
        return EMP_SHIFT_EMPLOYEE

    raw = t.split("(")[0].strip() if "(" in t else t
    parsed = parse_flexible_date(raw, TZ_MSK)
    if not parsed:
        await update.message.reply_text("⚠️ Не разобрал дату. Напишите число (например 15), 15.07, или нажмите Сегодня/Вчера:", reply_markup=get_step_keyboard())
        return EMP_SHIFT_DATE
    context.user_data["emp_date_iso"], context.user_data["emp_date"] = parsed
    await show_emp_shift_start(update, context)
    return EMP_SHIFT_START


async def emp_shift_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_emp_shift_date(update, context)
        return EMP_SHIFT_DATE
    parsed = parse_flexible_time(t)
    if not parsed:
        await update.message.reply_text("⚠️ Не понял время. Просто цифрами: 9, 09 или 900. Попробуйте ещё раз:", reply_markup=get_step_keyboard())
        return EMP_SHIFT_START
    context.user_data["emp_start"] = parsed
    await show_emp_shift_end(update, context)
    return EMP_SHIFT_END


async def emp_shift_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_emp_shift_start(update, context)
        return EMP_SHIFT_START
    parsed = parse_flexible_time(t)
    if not parsed:
        await update.message.reply_text("⚠️ Не понял время. Просто цифрами: 21 или 2100. Попробуйте ещё раз:", reply_markup=get_step_keyboard())
        return EMP_SHIFT_END

    t_start = datetime.strptime(context.user_data["emp_start"], "%H:%M")
    t_end = datetime.strptime(parsed, "%H:%M")
    hours = (t_end - t_start).total_seconds() / 3600
    if hours <= 0:
        hours += 24  # смена через полночь

    rate = context.user_data["emp_rate"]
    meal = MEAL_COMP_AMOUNT if hours >= MEAL_COMP_HOURS_THRESHOLD else 0
    pay = round(hours * rate, 2)
    total = round(pay + meal, 2)

    context.user_data["emp_end"] = parsed
    context.user_data["emp_hours"] = round(hours, 2)
    context.user_data["emp_pay"] = pay
    context.user_data["emp_meal"] = meal
    context.user_data["emp_total"] = total

    d = context.user_data
    meal_line = f"\n🍽 Обед: +{meal} ₽" if meal else ""
    summary = (
        f"🕐 *Смена: {d['emp_name']}*\n"
        f"📅 Дата: {d['emp_date']}\n"
        f"⏱ {d['emp_start']}–{d['emp_end']} ({d['emp_hours']} ч)\n"
        f"💰 Оплата: {pay} ₽{meal_line}\n"
        f"💰 *Итого начисление: {total} ₽*"
    )
    await update.message.reply_text(summary, reply_markup=ReplyKeyboardMarkup([["✅ Подтвердить"], ["🔙 Назад", "❌ Главное меню"]], resize_keyboard=True), parse_mode="Markdown")
    return EMP_SHIFT_CONFIRM


async def emp_shift_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "🔙 Назад":
        await show_emp_shift_end(update, context)
        return EMP_SHIFT_END
    if t != "✅ Подтвердить": return await cancel_to_menu(update, context)
    db, d = context.bot_data.get("db"), context.user_data

    category_id = db.get_category_id("Зарплата")
    comment = f"Смена {d['emp_date']} {d['emp_start']}–{d['emp_end']} ({d['emp_hours']}ч)"
    op_id = db.add_operation_returning_id(
        operation_date=d["emp_date_iso"],
        ip_id=None,
        counterparty_id=d["emp_id"],
        category_id=category_id,
        operation_type="начисление",
        amount=d["emp_total"],
        entered_by=str(update.effective_user.id),
        status="confirmed",
        payment_method=None,
        comment=comment,
    )
    db.post_journal_entry(
        operation_id=op_id, entry_date=d["emp_date_iso"],
        debit_code="5100", credit_code="2100",
        amount=d["emp_total"], comment=f"Начисление: {d['emp_name']} ({comment})",
    )
    kb = [["➕ Ещё смена (этот сотрудник)"], ["👤 Другой сотрудник", "✅ Готово"]]
    await update.message.reply_text(
        f"✅ Смена внесена, начислено {d['emp_total']} ₽ для {d['emp_name']}.\nВнести ещё одну смену?",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )
    return EMP_SHIFT_NEXT


async def emp_shift_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    db = context.bot_data.get("db")
    if t == "➕ Ещё смена (этот сотрудник)":
        await show_emp_shift_date(update, context)
        return EMP_SHIFT_DATE
    if t == "👤 Другой сотрудник":
        emps = [e["name"] for e in db.get_hourly_employees()]
        await update.message.reply_text("Шаг 1: Выберите сотрудника:", reply_markup=build_grid_keyboard(emps, columns=2))
        return EMP_SHIFT_EMPLOYEE
    return await cancel_to_menu(update, context)


# --- Начислить оклад (фиксированная зарплата) ---
async def show_emp_accrual_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["Ввести сумму вручную"], ["🔙 Назад", "❌ Главное меню"]]
    await update.message.reply_text("Шаг 2: Введите сумму начисления (например 120000):", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))


async def show_emp_accrual_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Комментарий (или '-'):", reply_markup=get_step_keyboard())


async def emp_accrual_employee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад": return await employee_start(update, context)
    db = context.bot_data.get("db")
    emp = db.get_counterparty_by_name(t)
    if not emp:
        await update.message.reply_text("Сотрудник не найден.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
        return ConversationHandler.END
    context.user_data["emp_name"] = t
    context.user_data["emp_id"] = emp["id"]
    await show_emp_accrual_amount(update, context)
    return EMP_ACCRUAL_AMOUNT


async def emp_accrual_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        db = context.bot_data.get("db")
        emps = [e["name"] for e in db.get_all_employees()]
        await update.message.reply_text("Шаг 1: Выберите сотрудника:", reply_markup=build_grid_keyboard(emps, columns=2))
        return EMP_ACCRUAL_EMPLOYEE
    if t == "Ввести сумму вручную":
        await update.message.reply_text("Введите сумму цифрами:", reply_markup=get_step_keyboard())
        return EMP_ACCRUAL_AMOUNT
    try: context.user_data["emp_amount"] = float(t.replace(",", ".").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("⚠️ Нужно ввести число, например 120000. Попробуйте ещё раз:", reply_markup=get_step_keyboard())
        return EMP_ACCRUAL_AMOUNT
    await show_emp_accrual_comment(update, context)
    return EMP_ACCRUAL_COMMENT


async def emp_accrual_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_emp_accrual_amount(update, context)
        return EMP_ACCRUAL_AMOUNT
    context.user_data["emp_comment"] = t if t != "-" else ""
    d = context.user_data
    summary = f"💵 *Начисление оклада:*\n👤 Сотрудник: {d['emp_name']}\n💰 Сумма: {d['emp_amount']} ₽"
    await update.message.reply_text(summary, reply_markup=ReplyKeyboardMarkup([["✅ Подтвердить"], ["🔙 Назад", "❌ Главное меню"]], resize_keyboard=True), parse_mode="Markdown")
    return EMP_ACCRUAL_CONFIRM


async def emp_accrual_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "🔙 Назад":
        await show_emp_accrual_comment(update, context)
        return EMP_ACCRUAL_COMMENT
    if t != "✅ Подтвердить": return await cancel_to_menu(update, context)
    db, d = context.bot_data.get("db"), context.user_data

    category_id = db.get_category_id("Зарплата")
    op_date = datetime.now(TZ_MSK).date().isoformat()
    op_id = db.add_operation_returning_id(
        operation_date=op_date,
        ip_id=None,
        counterparty_id=d["emp_id"],
        category_id=category_id,
        operation_type="начисление",
        amount=d["emp_amount"],
        entered_by=str(update.effective_user.id),
        status="confirmed",
        payment_method=None,
        comment=d["emp_comment"],
    )
    db.post_journal_entry(
        operation_id=op_id, entry_date=op_date,
        debit_code="5100", credit_code="2100",
        amount=d["emp_amount"], comment=f"Начисление оклада: {d['emp_name']}",
    )
    await update.message.reply_text(f"✅ Начислено {d['emp_amount']} ₽ для {d['emp_name']}.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


# --- ПРОДАЖА (реальная прибыль: по площадкам, по товарам, чистыми) ---
async def sale_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t_now = datetime.now(TZ_MSK)
    this_month_start = t_now.replace(day=1).strftime("%d.%m")
    kb = [[f"Этот месяц (с {this_month_start})"], ["Свой период (ДД.ММ-ДД.ММ)"], ["❌ Главное меню"]]
    await update.message.reply_text("💵 *Продажа — прибыль за период*\n\nЗа какой период?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")
    return SALE_IP


def format_full_profit_text(p: dict, date_from: str, date_to: str) -> str:
    lines = [f"💵 *Прибыль за период {date_from} — {date_to}*\n"]
    lines.append(f"💰 Выручка (реально от площадок, после всех их вычетов): *{p['revenue']} ₽*\n")

    lines.append("📦 *По товарам:*")
    for item in p["product_breakdown"]:
        lines.append(f"▫️ {item['name']}: {item['units']} шт | выручка {item['revenue']} ₽ | сырьё {item['cogs']} ₽ | расходники {item['packaging']} ₽ | маржа {item['margin']} ₽")
    lines.append(f"\n🌰 Себестоимость сырья всего: {p['cogs_total']} ₽")
    lines.append(f"📦 Расходники всего (по рецептуре, реальные цены): {p['packaging_total']} ₽\n")

    lines.append("📉 *Прочие расходы:*")
    for name, amt in p["expenses_by_category"]:
        lines.append(f"▫️ {name}: {round(amt,2)} ₽")
    lines.append(f"\n📉 Всего прочих расходов: {p['expenses_total']} ₽")
    lines.append(f"🏛 Налог (1%): {p['tax']} ₽")
    lines.append(f"🤝 Доля Булата (2%): {p['bulat_share']} ₽")

    profit_word = "Чистая прибыль" if p["net_profit"] >= 0 else "Убыток"
    lines.append(f"\n💰 *{profit_word}: {p['net_profit']} ₽*")
    return "\n".join(lines)


async def sale_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переиспользовано под выбор периода."""
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    db = context.bot_data.get("db")
    t_now = datetime.now(TZ_MSK)

    if "Этот месяц" in t:
        date_from = t_now.replace(day=1).date().isoformat()
        date_to = t_now.date().isoformat()
    else:
        await update.message.reply_text("Введите период в формате ДД.ММ-ДД.ММ (например 01.07-31.07):", reply_markup=get_step_keyboard())
        return SALE_MARKETPLACE

    p = db.get_full_period_profit(date_from, date_to)
    await update.message.reply_text(format_full_profit_text(p, date_from, date_to), reply_markup=get_main_menu_keyboard(update.effective_user.id), parse_mode="Markdown")
    return ConversationHandler.END


async def sale_marketplace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переиспользовано под ввод своего периода."""
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    db = context.bot_data.get("db")

    try:
        start_raw, end_raw = t.split("-")
        start_parsed = parse_flexible_date(start_raw.strip(), TZ_MSK)
        end_parsed = parse_flexible_date(end_raw.strip(), TZ_MSK)
        if not start_parsed or not end_parsed:
            raise ValueError
        date_from, date_to = start_parsed[0], end_parsed[0]
    except (ValueError, AttributeError):
        await update.message.reply_text("⚠️ Не понял период. Формат: ДД.ММ-ДД.ММ. Попробуйте ещё раз:", reply_markup=get_step_keyboard())
        return SALE_MARKETPLACE

    p = db.get_full_period_profit(date_from, date_to)
    await update.message.reply_text(format_full_profit_text(p, date_from, date_to), reply_markup=get_main_menu_keyboard(update.effective_user.id), parse_mode="Markdown")
    return ConversationHandler.END


# --- OZON SELLER API — синхронизация финансовых транзакций ---
async def fetch_ozon_transactions(client_id: str, api_key: str, date_from: str, date_to: str):
    """Забирает все финансовые транзакции Ozon за период (с пагинацией и повтором при обрыве соединения)."""
    headers = {"Client-Id": client_id, "Api-Key": api_key, "Content-Type": "application/json"}
    all_ops = []
    page = 1
    async with httpx.AsyncClient(timeout=30.0, http2=False) as http:
        while True:
            body = {
                "filter": {"date": {"from": f"{date_from}T00:00:00.000Z", "to": f"{date_to}T23:59:59.000Z"}, "transaction_type": "all"},
                "page": page,
                "page_size": 1000,
            }
            last_error = None
            for attempt in range(3):
                try:
                    resp = await http.post(f"{OZON_API_BASE}/v3/finance/transaction/list", headers=headers, json=body)
                    resp.raise_for_status()
                    data = resp.json()
                    last_error = None
                    break
                except (httpx.HTTPError, httpx.TransportError) as e:
                    last_error = e
                    logger.warning(f"Ozon API попытка {attempt+1}/3 не удалась (стр. {page}): {e}")
            if last_error:
                raise last_error

            ops = data.get("result", {}).get("operations", [])
            all_ops.extend(ops)
            page_count = data.get("result", {}).get("page_count", 1)
            if page >= page_count or not ops:
                break
            page += 1
    return all_ops


async def sync_ozon_transactions(db: "SupabaseService", ip_name: str, client_id: str, api_key: str, date_from: str, date_to: str) -> int:
    ops = await fetch_ozon_transactions(client_id, api_key, date_from, date_to)
    ip_id = db.get_ip_id(ip_name)
    batch = []
    count = 0
    BATCH_SIZE = 500
    for op in ops:
        posting = op.get("posting") or {}
        items = op.get("items") or []
        item_name = items[0].get("name") if items else None
        sku = items[0].get("sku") if items else None
        op_date = (op.get("operation_date") or "")[:10] or date_from
        batch.append({
            "ozon_operation_id": op.get("operation_id"),
            "ip_id": ip_id,
            "operation_date": op_date,
            "operation_type": op.get("operation_type"),
            "operation_type_name": op.get("operation_type_name"),
            "posting_number": posting.get("posting_number"),
            "sku": sku,
            "item_name": item_name,
            "amount": op.get("amount", 0),
            "accruals_for_sale": op.get("accruals_for_sale"),
            "commission_amount": op.get("sale_commission"),
            "delivery_charge": (op.get("delivery_charge") or {}).get("amount"),
            "return_delivery_charge": (op.get("return_delivery_charge") or {}).get("amount"),
            "raw_json": op,
        })
        if len(batch) >= BATCH_SIZE:
            db.upsert_ozon_transactions_batch(batch)
            count += len(batch)
            batch = []
    if batch:
        db.upsert_ozon_transactions_batch(batch)
        count += len(batch)
    return count


async def run_ozon_sync_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневная автоматическая синхронизация — последние 3 дня (захватывает возможные корректировки)."""
    if not OZON_BULAT_CLIENT_ID or not OZON_BULAT_API_KEY:
        return
    if context.bot_data.get("ozon_sync_running"):
        logger.info("Ozon sync: пропускаю плановый запуск — уже идёт другая синхронизация.")
        return
    context.bot_data["ozon_sync_running"] = True
    db = context.bot_data.get("db")
    date_to = datetime.now(TZ_MSK).date().isoformat()
    date_from = (datetime.now(TZ_MSK).date() - timedelta(days=3)).isoformat()
    try:
        count = await sync_ozon_transactions(db, "Булат", OZON_BULAT_CLIENT_ID, OZON_BULAT_API_KEY, date_from, date_to)
        logger.info(f"Ozon sync: обновлено {count} транзакций за {date_from}–{date_to}")
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"🔄 Ozon: синхронизировано {count} транзакций за {date_from} — {date_to}")
    except Exception as e:
        logger.error(f"Ozon sync failed: {e}")
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Ошибка синхронизации Ozon: {e}")
    finally:
        context.bot_data["ozon_sync_running"] = False


async def run_fixed_costs_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневная проверка: не пора ли автоматически начислить аренду (1 числа) или пропуск (раз в ~3 месяца)."""
    db = context.bot_data.get("db")
    today = datetime.now(TZ_MSK).date()
    rent_cat_id = db.get_category_id("Аренда склада")
    propusk_cat_id = db.get_category_id("Пропуск")

    if today.day == 1:
        existing = (
            db.client.table("operations").select("id")
            .eq("category_id", rent_cat_id)
            .gte("operation_date", today.isoformat())
            .execute().data or []
        )
        if not existing:
            for amount, method in [(22000, "Безнал ИП"), (38000, "Наличные")]:
                op_id = db.add_operation_returning_id(
                    operation_date=today.isoformat(), ip_id=None, counterparty_id=None,
                    category_id=rent_cat_id, operation_type="расход", amount=amount,
                    entered_by="auto_monthly", status="confirmed", payment_method=method,
                    comment="Автоначисление: аренда склада Fud City",
                )
                cash_code = "1000" if method == "Наличные" else "1010"
                db.post_journal_entry(operation_id=op_id, entry_date=today.isoformat(), debit_code="5900", credit_code=cash_code, amount=amount, comment="Аренда склада (авто)")
            await context.bot.send_message(chat_id=ADMIN_ID, text="💸 Автоматически начислена аренда склада: 22 000₽ безнал + 38 000₽ наличные.")

    last_propusk = (
        db.client.table("operations").select("operation_date")
        .eq("category_id", propusk_cat_id)
        .order("operation_date", desc=True).limit(1)
        .execute().data or []
    )
    days_since = 9999 if not last_propusk else (today - datetime.strptime(last_propusk[0]["operation_date"], "%Y-%m-%d").date()).days
    if days_since >= 90:
        op_id = db.add_operation_returning_id(
            operation_date=today.isoformat(), ip_id=None, counterparty_id=None,
            category_id=propusk_cat_id, operation_type="расход", amount=5000,
            entered_by="auto_quarterly", status="confirmed", payment_method="Безнал ИП",
            comment="Автоначисление: пропуск (Бахтовар + Диловар, на 3 мес.)",
        )
        db.post_journal_entry(operation_id=op_id, entry_date=today.isoformat(), debit_code="5900", credit_code="1010", amount=5000, comment="Пропуск (авто)")
        await context.bot.send_message(chat_id=ADMIN_ID, text="💸 Автоматически начислен пропуск: 5 000₽ (прошло 90+ дней с прошлого раза).")


async def _run_ozon_sync_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, date_from: str, date_to: str):
    if context.bot_data.get("ozon_sync_running"):
        await update.message.reply_text("⏳ Синхронизация уже идёт, подождите её завершения — не нажимайте повторно.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
        return
    context.bot_data["ozon_sync_running"] = True
    await update.message.reply_text(f"🔄 Синхронизирую данные с Ozon за {date_from} — {date_to}, подождите...", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    db = context.bot_data.get("db")
    try:
        count = await sync_ozon_transactions(db, "Булат", OZON_BULAT_CLIENT_ID, OZON_BULAT_API_KEY, date_from, date_to)
        await update.message.reply_text(f"✅ Готово: синхронизировано {count} транзакций за {date_from} — {date_to}.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка синхронизации: {e}", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    finally:
        context.bot_data["ozon_sync_running"] = False


async def ozon_sync_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1: выбор периода синхронизации (только админ)."""
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    if not OZON_BULAT_CLIENT_ID or not OZON_BULAT_API_KEY:
        await update.message.reply_text("⚠️ Ключи Ozon ещё не настроены в переменных окружения.")
        return ConversationHandler.END
    t_now = datetime.now(TZ_MSK)
    this_month_start = t_now.replace(day=1).strftime("%d.%m")
    kb = [["Последние 30 дней"], [f"Этот месяц (с {this_month_start})"], ["Свой период (ДД.ММ-ДД.ММ)"], ["❌ Главное меню"]]
    await update.message.reply_text("🔄 *Синхронизация Ozon*\n\nЗа какой период?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")
    return OZON_SYNC_PERIOD


async def ozon_sync_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    t_now = datetime.now(TZ_MSK)

    if t == "Последние 30 дней":
        date_from = (t_now.date() - timedelta(days=30)).isoformat()
        date_to = t_now.date().isoformat()
        await _run_ozon_sync_and_reply(update, context, date_from, date_to)
        return ConversationHandler.END

    if "Этот месяц" in t:
        date_from = t_now.replace(day=1).date().isoformat()
        date_to = t_now.date().isoformat()
        await _run_ozon_sync_and_reply(update, context, date_from, date_to)
        return ConversationHandler.END

    if "Свой период" in t:
        await update.message.reply_text("Введите период в формате ДД.ММ-ДД.ММ (например 01.07-31.07):", reply_markup=get_step_keyboard())
        return OZON_SYNC_PERIOD_CUSTOM

    this_month_start = t_now.replace(day=1).strftime("%d.%m")
    kb = [["Последние 30 дней"], [f"Этот месяц (с {this_month_start})"], ["Свой период (ДД.ММ-ДД.ММ)"], ["❌ Главное меню"]]
    await update.message.reply_text("⚠️ Не понял выбор. Нажмите один из вариантов на клавиатуре:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return OZON_SYNC_PERIOD


async def ozon_sync_period_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    try:
        start_raw, end_raw = t.split("-")
        start_parsed = parse_flexible_date(start_raw.strip(), TZ_MSK)
        end_parsed = parse_flexible_date(end_raw.strip(), TZ_MSK)
        if not start_parsed or not end_parsed:
            raise ValueError
        date_from, date_to = start_parsed[0], end_parsed[0]
    except (ValueError, AttributeError):
        await update.message.reply_text("⚠️ Не понял период. Формат: ДД.ММ-ДД.ММ, например 01.07-31.07. Попробуйте ещё раз:", reply_markup=get_step_keyboard())
        return OZON_SYNC_PERIOD_CUSTOM

    await _run_ozon_sync_and_reply(update, context, date_from, date_to)
    return ConversationHandler.END


# --- OZON ОТЗЫВЫ И ВОПРОСЫ (Reviews & Questions) ---
async def _ozon_api_post(client_id: str, api_key: str, path: str, body: dict) -> dict:
    """Общий POST к Ozon Seller API с ретраями — как в fetch_ozon_transactions."""
    headers = {"Client-Id": client_id, "Api-Key": api_key, "Content-Type": "application/json"}
    last_error = None
    async with httpx.AsyncClient(timeout=30.0, http2=False) as http:
        for attempt in range(3):
            try:
                resp = await http.post(f"{OZON_API_BASE}{path}", headers=headers, json=body)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                last_error = e
                logger.warning(f"Ozon API попытка {attempt+1}/3 не удалась ({path}): {e.response.status_code} {e.response.text}")
                if e.response.status_code < 500:
                    break  # клиентская ошибка (400/401/403) — повтор того же запроса не поможет
            except httpx.TransportError as e:
                last_error = e
                logger.warning(f"Ozon API попытка {attempt+1}/3 не удалась ({path}): {e}")
    raise last_error


async def fetch_ozon_reviews(client_id: str, api_key: str, date_from: str, date_to: str) -> list:
    """Список отзывов за период. Точные поля пагинации/фильтра сверим на первом реальном запуске."""
    all_reviews = []
    last_id = ""
    while True:
        data = await _ozon_api_post(client_id, api_key, "/v1/review/list", {
            "last_id": last_id, "limit": 100, "sort_dir": "ASC",
        })
        result = data.get("result") or data
        reviews = result.get("reviews", [])
        reviews = [r for r in reviews if date_from <= (r.get("published_at") or "")[:10] <= date_to]
        all_reviews.extend(reviews)
        last_id = result.get("last_id", "")
        if not result.get("has_next") or not last_id:
            break
    return all_reviews


async def fetch_ozon_review_info(client_id: str, api_key: str, review_id) -> dict:
    """Полная карточка отзыва: текст, фото, видео, рейтинг, sku, автор."""
    data = await _ozon_api_post(client_id, api_key, "/v1/review/info", {"review_id": review_id})
    return data.get("result") or data


async def fetch_ozon_questions(client_id: str, api_key: str, date_from: str, date_to: str) -> list:
    """Список вопросов покупателей за период."""
    all_questions = []
    last_id = ""
    while True:
        data = await _ozon_api_post(client_id, api_key, "/v1/question/list", {
            "last_id": last_id, "limit": 100,
            "filter": {"date_from": f"{date_from}T00:00:00.000Z", "date_to": f"{date_to}T23:59:59.000Z"},
        })
        result = data.get("result") or data
        questions = result.get("questions", [])
        all_questions.extend(questions)
        last_id = result.get("last_id", "")
        if not result.get("has_next") or not last_id:
            break
    return all_questions


async def fetch_ozon_product_names(client_id: str, api_key: str, skus: list) -> dict:
    """Названия товаров по SKU — одним батч-запросом. Точные поля сверим на первом реальном запуске."""
    unique_skus = list({s for s in skus if s})
    if not unique_skus:
        return {}
    data = await _ozon_api_post(client_id, api_key, "/v3/product/info/list", {"sku": unique_skus})
    items = data.get("items") or (data.get("result") or {}).get("items") or []
    return {item.get("sku"): item.get("name") for item in items if item.get("sku")}


async def publish_ozon_review_comment(client_id: str, api_key: str, review_id, text: str):
    await _ozon_api_post(client_id, api_key, "/v1/review/comment/create", {
        "review_id": review_id, "text": text, "mark_review_as_processed": True,
    })


async def publish_ozon_question_answer(client_id: str, api_key: str, question_id, sku, text: str):
    await _ozon_api_post(client_id, api_key, "/v1/question/answer/create", {
        "question_id": question_id, "sku": sku, "text": text,
    })


def _review_has_media(info: dict) -> bool:
    photos = info.get("photos") or info.get("photos_amount") or []
    videos = info.get("videos") or info.get("videos_amount") or []
    photos_count = len(photos) if isinstance(photos, list) else int(photos or 0)
    videos_count = len(videos) if isinstance(videos, list) else int(videos or 0)
    return photos_count > 0 or videos_count > 0


async def generate_feedback_response(feedback_type: str, text_content: str, rating=None, product_hint: str = None) -> str:
    """Индивидуальный ответ через Claude — заново под конкретный текст, без подстановки в шаблон."""
    kind_label = "отзыв на товар" if feedback_type == "review" else "вопрос покупателя о товаре"
    rating_line = f"Оценка покупателя: {rating}/5.\n" if rating is not None else ""
    product_line = f"Товар: {product_hint}.\n" if product_hint else ""
    system_prompt = (
        "Ты — представитель бренда на маркетплейсе Ozon, отвечаешь покупателям от лица магазина. "
        "Пиши по-русски, спокойно и по-деловому, обращайся к конкретным деталям, которые упомянул покупатель — "
        "не используй шаблонные фразы вроде 'Спасибо за отзыв, нам важно ваше мнение'. Всегда начинай ответ с "
        "короткого приветствия ('Здравствуйте!' или 'Добрый день!') — и в положительных отзывах, и в "
        "отрицательных, и в ответах на вопросы. Длина ответа — примерно 4 предложения, не длиннее и не короче.\n\n"
        "СПРАВОЧНИК ПО ТОВАРАМ (только для твоего понимания при формулировке ответа — никогда не заявляй эти "
        "факты прямо и категорично покупателю, это читают и другие покупатели, формулируй мягко и по существу):\n"
        "- Чернослив: маслянистая/влажная поверхность — естественная характеристика сухофрукта, не дефект.\n"
        "- Изюм Малаяр: 100% чистый натуральный состав. Если жалуются на масло — не подтверждай и не отрицай "
        "его наличие явно и прямо, отвечай нейтрально про натуральность состава.\n"
        "- Изюм Терма: 100% чистый натуральный состав, масла в составе нет.\n"
        "- Боярышник: 100% чистый натуральный состав.\n"
        "- Шиповник: 100% чистый натуральный состав.\n"
        "- Фасоль: 100% чистый натуральный состав.\n"
        "Для Терма, Боярышника, Шиповника и Фасоли масла в составе нет — если покупатель жалуется на масло "
        "именно по этим товарам, это НЕ спишешь на естественную характеристику, это повод разобраться как с "
        "реальной проблемой (см. пункт 1 ниже).\n\n"
        "Если это негативный отзыв или жалоба на товар, сначала определи её тип:\n"
        "1) РЕАЛЬНЫЙ БРАК ИЛИ ПРОБЛЕМА КАЧЕСТВА — товар испорчен, плесень, инородный предмет, прислали не тот "
        "товар, повреждённая упаковка при доставке, а также жалоба на характеристику, которой по справочнику "
        "выше не должно быть у этого товара. Признай проблему и извинись по существу, но НЕ обещай конкретный "
        "исход (не пиши 'оформим возврат' или 'заменим товар') — предложи написать в личные сообщения магазина "
        "с фото товара и номером заказа, чтобы разобраться и помочь; решение по возврату или замене "
        "обсуждается уже там. Ориентируйся на тон и длину этого примера (не копируй дословно, формулируй "
        "заново под конкретный отзыв): 'Здравствуйте! Очень жаль это слышать, такого точно быть не должно, "
        "приносим извинения за некачественную партию. Пожалуйста, напишите нам в личные сообщения магазина с "
        "фото товара и номером заказа — разберёмся и поможем. Хотим понять, что пошло не так, и исправить "
        "ситуацию.'\n"
        "2) ЕСТЕСТВЕННЫЕ ХАРАКТЕРИСТИКИ ТОВАРА, которые покупатель может принять за недостаток (см. справочник "
        "выше) — например, вариативность размера и формы плодов (натуральный продукт, не откалиброванный по "
        "ГОСТу как конфеты), и подобное. Для этого типа: не извиняйся, как будто это наша ошибка; не отрицай "
        "сам факт, если он действительно есть, но и не подтверждай его прямо и категорично; не заостряй на "
        "этом лишнее внимание и не оправдывайся; спокойно и по-деловому объясни, что это особенность продукта. "
        "НЕ предлагай возврат или замену по умолчанию для этого типа — только если покупатель сам явно просит "
        "вернуть товар.\n\n"
        "Общий принцип: если жалоба основана на неверном представлении о характеристиках продукта — вежливо, но "
        "твёрдо не соглашайся с такой трактовкой, объясняя факты по существу. Не заискивай и не следуй правилу "
        "'клиент всегда прав' там, где покупатель просто не так понял свойства товара.\n\n"
        "Если это вопрос — ответь по существу; если в тексте недостаточно данных для точного ответа, честно скажи, "
        "что уточнишь детали, и предложи написать в личные сообщения магазина.\n\n"
        "Без markdown-разметки, без подписи в конце."
    )
    user_prompt = f"{product_line}{rating_line}Текст покупателя ({kind_label}):\n{text_content or '(текста нет)'}"

    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 400,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    last_error = None
    async with httpx.AsyncClient(timeout=30.0, http2=False) as http:
        for attempt in range(3):
            try:
                resp = await http.post(ANTHROPIC_API_BASE, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
                return "".join(block.get("text", "") for block in data.get("content", [])).strip()
            except httpx.HTTPStatusError as e:
                last_error = e
                logger.warning(f"Claude API попытка {attempt+1}/3 не удалась: {e.response.status_code} {e.response.text}")
                if e.response.status_code < 500:
                    break  # клиентская ошибка (400/401/403) — повтор того же запроса не поможет
            except httpx.TransportError as e:
                last_error = e
                logger.warning(f"Claude API попытка {attempt+1}/3 не удалась: {e}")
    raise last_error


def _build_feedback_row(feedback_type: str, ozon_id, sku, rating, author_name, text_content, product_name, raw_json: dict) -> dict:
    return {
        "feedback_type": feedback_type,
        "ozon_id": str(ozon_id),
        "sku": sku,
        "rating": rating,
        "author_name": author_name,
        "text_content": text_content,
        "product_name": product_name,
        "status": "new",
        "raw_json": raw_json,
    }


async def collect_new_feedback_rows(db: "SupabaseService", client_id: str, api_key: str, date_from: str, date_to: str) -> list:
    """Тянет отзывы и вопросы за период, отбрасывает уже сохранённые (по ozon_id) — возвращает строки для insert."""
    candidates = []
    for r in await fetch_ozon_reviews(client_id, api_key, date_from, date_to):
        candidates.append(("review", r))
    for q in await fetch_ozon_questions(client_id, api_key, date_from, date_to):
        candidates.append(("question", q))

    all_ids = [str(item.get("id") if kind == "review" else item.get("question_id")) for kind, item in candidates]
    existing_ids = db.get_existing_ozon_feedback_ids(all_ids)
    new_candidates = [
        (kind, item) for kind, item in candidates
        if str(item.get("id") if kind == "review" else item.get("question_id")) not in existing_ids
    ]

    review_infos = {}
    for kind, item in new_candidates:
        if kind == "review":
            review_infos[item.get("id")] = await fetch_ozon_review_info(client_id, api_key, item.get("id"))

    skus = set()
    for kind, item in new_candidates:
        sku = (review_infos[item.get("id")].get("sku") if kind == "review" else item.get("sku")) or item.get("sku")
        if sku:
            skus.add(sku)
    product_names = await fetch_ozon_product_names(client_id, api_key, list(skus))

    rows = []
    for kind, item in new_candidates:
        ozon_id = str(item.get("id") if kind == "review" else item.get("question_id"))
        if kind == "review":
            info = review_infos[item.get("id")]
            sku = info.get("sku") or item.get("sku")
            rows.append(_build_feedback_row(
                "review", ozon_id, sku, info.get("rating") or item.get("rating"),
                info.get("author_name") or info.get("user_name") or item.get("author_name"),
                info.get("text") or item.get("text"), product_names.get(sku), {"list": item, "info": info},
            ))
        else:
            sku = item.get("sku")
            rows.append(_build_feedback_row(
                "question", ozon_id, sku, None,
                item.get("author_name") or item.get("user_name"), item.get("text"),
                product_names.get(sku), item,
            ))
    return rows


def _feedback_preview_text(row: dict, response_text: str) -> str:
    kind_label = "⭐ Отзыв" if row["feedback_type"] == "review" else "❓ Вопрос"
    rating_line = f"\nОценка: {row.get('rating')}/5" if row.get("rating") is not None else ""
    product_label = row.get("product_name") or (f"SKU {row['sku']}" if row.get("sku") else "—")
    return (
        f"{kind_label} на Ozon{rating_line}\n"
        f"👤 {row.get('author_name') or 'Аноним'}\n"
        f"📦 Товар: {product_label}\n\n"
        f"💬 Текст покупателя:\n{row.get('text_content') or '(без текста)'}\n\n"
        f"🤖 Черновик ответа:\n{response_text}"
    )


def _feedback_approval_keyboard(feedback_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Опубликовать", callback_data=f"ozfb_pub_{feedback_id}"),
        InlineKeyboardButton("✏️ Править", callback_data=f"ozfb_edit_{feedback_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"ozfb_rej_{feedback_id}"),
    ]])


async def send_feedback_for_approval(context: ContextTypes.DEFAULT_TYPE, db: "SupabaseService", feedback_id):
    row = db.get_ozon_feedback(feedback_id)
    text = _feedback_preview_text(row, row.get("draft_response") or "")
    msg = await context.bot.send_message(chat_id=ADMIN_ID, text=text, reply_markup=_feedback_approval_keyboard(feedback_id))
    db.update_ozon_feedback(feedback_id, status="pending_approval", telegram_message_id=msg.message_id)


async def process_new_feedback_item(db: "SupabaseService", context: ContextTypes.DEFAULT_TYPE, row: dict):
    feedback_type = row["feedback_type"]
    text_content = row.get("text_content") or ""
    rating = row.get("rating")

    if feedback_type == "review" and rating is not None and rating >= 4:
        info = (row.get("raw_json") or {}).get("info") or {}
        has_content = bool(text_content.strip()) or _review_has_media(info)
        if not has_content:
            db.update_ozon_feedback(row["id"], status="skipped")
            return
        response_text = await generate_feedback_response("review", text_content, rating, row.get("product_name"))
        await publish_ozon_review_comment(OZON_BULAT_CLIENT_ID, OZON_BULAT_API_KEY, row["ozon_id"], response_text)
        db.update_ozon_feedback(
            row["id"], status="published", draft_response=response_text,
            final_response=response_text, published_at=datetime.now(TZ_MSK).isoformat(),
        )
        return

    # Негативный отзыв (rating < 4) или любой вопрос — только черновик, публикация после подтверждения админом
    response_text = await generate_feedback_response(feedback_type, text_content, rating, row.get("product_name"))
    db.update_ozon_feedback(row["id"], status="draft_ready", draft_response=response_text)
    await send_feedback_for_approval(context, db, row["id"])


async def _run_ozon_feedback_sync(db: "SupabaseService", context: ContextTypes.DEFAULT_TYPE, date_from: str, date_to: str) -> tuple:
    """Возвращает (успешно_обработано, ошибок) — счётчики раздельные, чтобы не репортить ложный успех."""
    rows = await collect_new_feedback_rows(db, OZON_BULAT_CLIENT_ID, OZON_BULAT_API_KEY, date_from, date_to)
    db.insert_ozon_feedback_batch(rows)
    new_items = db.get_new_ozon_feedback()
    success_count = 0
    error_count = 0
    for item in new_items:
        try:
            await process_new_feedback_item(db, context, item)
            success_count += 1
        except Exception:
            error_count += 1
            logger.exception(f"Ошибка обработки отзыва/вопроса id={item.get('id')} ozon_id={item.get('ozon_id')}")
    return success_count, error_count


async def run_ozon_feedback_sync_job(context: ContextTypes.DEFAULT_TYPE):
    """Плановая проверка новых отзывов/вопросов Ozon — каждые OZON_FEEDBACK_SYNC_MINUTES минут."""
    if not (OZON_BULAT_CLIENT_ID and OZON_BULAT_API_KEY and ANTHROPIC_API_KEY):
        return
    if context.bot_data.get("ozon_feedback_sync_running"):
        logger.info("Ozon feedback sync: пропускаю плановый запуск — уже идёт другая синхронизация.")
        return
    context.bot_data["ozon_feedback_sync_running"] = True
    db = context.bot_data.get("db")
    date_to = datetime.now(TZ_MSK).date().isoformat()
    date_from = (datetime.now(TZ_MSK).date() - timedelta(days=2)).isoformat()
    try:
        success_count, error_count = await _run_ozon_feedback_sync(db, context, date_from, date_to)
        if success_count or error_count:
            logger.info(f"Ozon feedback sync: обработано {success_count}, ошибок {error_count}")
        if error_count:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Синхронизация отзывов/вопросов Ozon: обработано {success_count}, ошибок {error_count} (детали — в логах).")
    except Exception:
        logger.exception("Ozon feedback sync failed")
        await context.bot.send_message(chat_id=ADMIN_ID, text="⚠️ Ошибка синхронизации отзывов/вопросов Ozon (детали — в логах).")
    finally:
        context.bot_data["ozon_feedback_sync_running"] = False


async def _run_ozon_feedback_sync_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, date_from: str, date_to: str):
    if context.bot_data.get("ozon_feedback_sync_running"):
        await update.message.reply_text("⏳ Синхронизация отзывов уже идёт, подождите её завершения.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
        return
    context.bot_data["ozon_feedback_sync_running"] = True
    await update.message.reply_text(f"🔄 Проверяю отзывы и вопросы Ozon за {date_from} — {date_to}, подождите...", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    db = context.bot_data.get("db")
    try:
        success_count, error_count = await _run_ozon_feedback_sync(db, context, date_from, date_to)
        if error_count:
            await update.message.reply_text(
                f"⚠️ За {date_from} — {date_to}: обработано {success_count}, ошибок {error_count}. Подробности — в логах бота.",
                reply_markup=get_main_menu_keyboard(update.effective_user.id),
            )
        else:
            await update.message.reply_text(f"✅ Готово: обработано {success_count} новых отзывов/вопросов за {date_from} — {date_to}.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    except Exception as e:
        logger.exception("Ручная синхронизация отзывов/вопросов Ozon failed")
        await update.message.reply_text(f"⚠️ Ошибка синхронизации: {e}", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    finally:
        context.bot_data["ozon_feedback_sync_running"] = False


async def ozon_feedback_sync_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1: выбор периода ручной проверки отзывов/вопросов (только админ)."""
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    if not (OZON_BULAT_CLIENT_ID and OZON_BULAT_API_KEY and ANTHROPIC_API_KEY):
        await update.message.reply_text("⚠️ Ключи Ozon и/или Anthropic ещё не настроены в переменных окружения.")
        return ConversationHandler.END
    kb = [["Последние 3 дня"], ["Последние 30 дней"], ["Свой период (ДД.ММ-ДД.ММ)"], ["❌ Главное меню"]]
    await update.message.reply_text("🔄 *Синхронизация отзывов/вопросов Ozon*\n\nЗа какой период?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")
    return OZON_FEEDBACK_SYNC_PERIOD


async def ozon_feedback_sync_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    t_now = datetime.now(TZ_MSK)

    if t == "Последние 3 дня":
        date_from = (t_now.date() - timedelta(days=3)).isoformat()
        date_to = t_now.date().isoformat()
        await _run_ozon_feedback_sync_and_reply(update, context, date_from, date_to)
        return ConversationHandler.END

    if t == "Последние 30 дней":
        date_from = (t_now.date() - timedelta(days=30)).isoformat()
        date_to = t_now.date().isoformat()
        await _run_ozon_feedback_sync_and_reply(update, context, date_from, date_to)
        return ConversationHandler.END

    if "Свой период" in t:
        await update.message.reply_text("Введите период в формате ДД.ММ-ДД.ММ (например 01.07-31.07):", reply_markup=get_step_keyboard())
        return OZON_FEEDBACK_SYNC_PERIOD_CUSTOM

    kb = [["Последние 3 дня"], ["Последние 30 дней"], ["Свой период (ДД.ММ-ДД.ММ)"], ["❌ Главное меню"]]
    await update.message.reply_text("⚠️ Не понял выбор. Нажмите один из вариантов на клавиатуре:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return OZON_FEEDBACK_SYNC_PERIOD


async def ozon_feedback_sync_period_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    try:
        start_raw, end_raw = t.split("-")
        start_parsed = parse_flexible_date(start_raw.strip(), TZ_MSK)
        end_parsed = parse_flexible_date(end_raw.strip(), TZ_MSK)
        if not start_parsed or not end_parsed:
            raise ValueError
        date_from, date_to = start_parsed[0], end_parsed[0]
    except (ValueError, AttributeError):
        await update.message.reply_text("⚠️ Не понял период. Формат: ДД.ММ-ДД.ММ, например 01.07-31.07. Попробуйте ещё раз:", reply_markup=get_step_keyboard())
        return OZON_FEEDBACK_SYNC_PERIOD_CUSTOM

    await _run_ozon_feedback_sync_and_reply(update, context, date_from, date_to)
    return ConversationHandler.END


async def feedback_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопки '✅ Опубликовать' / '❌ Отклонить' под черновиком ответа."""
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != ADMIN_ID:
        return
    _, action, feedback_id_str = query.data.split("_", 2)
    feedback_id = int(feedback_id_str)
    db = context.bot_data.get("db")
    row = db.get_ozon_feedback(feedback_id)
    if not row or row["status"] != "pending_approval":
        await query.edit_message_text("⚠️ Эта запись уже обработана или не найдена.")
        return

    if action == "rej":
        db.update_ozon_feedback(feedback_id, status="rejected", approved_by=str(update.effective_user.id), approved_at=datetime.now(TZ_MSK).isoformat())
        await query.edit_message_text(query.message.text + "\n\n❌ Отклонено.")
        return

    response_text = row.get("final_response") or row.get("draft_response") or ""
    try:
        if row["feedback_type"] == "review":
            await publish_ozon_review_comment(OZON_BULAT_CLIENT_ID, OZON_BULAT_API_KEY, row["ozon_id"], response_text)
        else:
            await publish_ozon_question_answer(OZON_BULAT_CLIENT_ID, OZON_BULAT_API_KEY, row["ozon_id"], row.get("sku"), response_text)
    except Exception as e:
        await query.edit_message_text(query.message.text + f"\n\n⚠️ Ошибка публикации в Ozon: {e}")
        return

    db.update_ozon_feedback(
        feedback_id, status="published", final_response=response_text,
        approved_by=str(update.effective_user.id), approved_at=datetime.now(TZ_MSK).isoformat(),
        published_at=datetime.now(TZ_MSK).isoformat(),
    )
    await query.edit_message_text(query.message.text + "\n\n✅ Опубликовано.")


async def feedback_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка '✏️ Править' — просим админа прислать новый текст ответа."""
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    feedback_id = int(query.data.split("_", 2)[2])
    row = context.bot_data.get("db").get_ozon_feedback(feedback_id)
    if not row or row["status"] != "pending_approval":
        await query.edit_message_text("⚠️ Эта запись уже обработана или не найдена.")
        return ConversationHandler.END
    context.user_data["fb_edit_id"] = feedback_id
    await query.message.reply_text(f"✏️ Пришлите новый текст ответа вместо:\n\n{row.get('draft_response') or ''}")
    return FEEDBACK_EDIT_TEXT


async def feedback_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    feedback_id = context.user_data.get("fb_edit_id")
    new_text = update.message.text.strip()
    db = context.bot_data.get("db")
    db.update_ozon_feedback(feedback_id, final_response=new_text)
    row = db.get_ozon_feedback(feedback_id)
    await update.message.reply_text(_feedback_preview_text(row, new_text), reply_markup=_feedback_approval_keyboard(feedback_id))
    return ConversationHandler.END


# --- OZON ПРИЁМКА ПОСТАВОК — списание склада по факту приёмки (не по продажам) ---
# SKU -> внутреннее название товара (стабильный маппинг, 6 позиций)
SKU_TO_PRODUCT_NAME = {
    4839976987: "Боярышник",
    4763890174: "Изюм Малаяр",
    4839943679: "Изюм Терма",
    4764007245: "Фасоль красная",
    4997584811: "Чернослив",
    4839932178: "Шиповник",
}


# ВРЕМЕННО (отладка /v3/supply-order/list и /get) — сырые ответы для показа в Telegram, убрать вместе с блоком ниже
_DEBUG_LAST_SUPPLY_LIST_RESPONSE = None
_DEBUG_LAST_SUPPLY_GET_RESPONSE = None


async def fetch_ozon_supply_order_ids(client_id: str, api_key: str, date_from: str, date_to: str) -> list:
    """ID поставок за период. Реальный ответ подтвердил структуру: {"order_ids": [int, int, ...]} прямо в
    корне — БЕЗ обёртки "result" и без обёртки-объекта на каждую поставку (никаких supply_orders/orders/items,
    как предполагалось раньше). /v3/supply-order/list отдаёт только ID; детали (номер, статус, состав) —
    отдельным вызовом /v3/supply-order/get, см. fetch_ozon_supply_order_details.

    sort_by=1 и filter.states=[1..11] подтверждены реальными ответами Ozon (прошли валидацию).
    Пагинация (last_id/has_next) в подтверждённом ответе не видна — возможно, её просто нет для этого
    метода (весь список приходит одним ответом), возможно поля называются иначе. ⚠️ best-effort, сверим
    по факту следующего реального запроса, если понадобится пагинация."""
    global _DEBUG_LAST_SUPPLY_LIST_RESPONSE
    data = await _ozon_api_post(client_id, api_key, "/v3/supply-order/list", {
        "filter": {"since": f"{date_from}T00:00:00.000Z", "to": f"{date_to}T23:59:59.000Z", "states": OZON_SUPPLY_STATES_PROBE},
        "sort_by": 1, "last_id": "", "limit": 100,
    })
    _DEBUG_LAST_SUPPLY_LIST_RESPONSE = data  # ВРЕМЕННО
    return data.get("order_ids") or (data.get("result") or {}).get("order_ids") or []


async def fetch_ozon_supply_order_details(client_id: str, api_key: str, order_ids: list) -> list:
    """Детали поставок по ID через /v3/supply-order/get (та же схема ключа "order_ids", что подтвердилась
    у /list — берём по аналогии, форма ответа пока НЕ проверена). ⚠️ best-effort, сверим по факту запроса."""
    global _DEBUG_LAST_SUPPLY_GET_RESPONSE
    if not order_ids:
        return []
    data = await _ozon_api_post(client_id, api_key, "/v3/supply-order/get", {"order_ids": order_ids})
    _DEBUG_LAST_SUPPLY_GET_RESPONSE = data  # ВРЕМЕННО
    result = data.get("result") or data
    return result.get("orders") or result.get("supply_orders") or result.get("items") or []


async def fetch_ozon_supply_order_bundle(client_id: str, api_key: str, order_id) -> list:
    """Состав поставки по SKU через /v1/supply-order/bundle. Поле с фактическим количеством не подтверждено —
    сверим на первом реальном запросе (пробуем несколько вероятных имён полей)."""
    data = await _ozon_api_post(client_id, api_key, "/v1/supply-order/bundle", {"supply_order_id": order_id})
    result = data.get("result") or data
    return result.get("items") or result.get("bundle") or []


async def collect_supply_acceptance_lines(client_id: str, api_key: str, date_from: str, date_to: str) -> tuple:
    """Возвращает (lines, status_counts). Фильтр по статусу поставки сознательно НЕ делаем — статус пока не
    проверен на реальных данных, поэтому просто собираем сырую статистику по статусам и показываем админу,
    чтобы решить вместе, какие статусы считать 'завершено', вместо того чтобы гадать заранее."""
    order_ids = await fetch_ozon_supply_order_ids(client_id, api_key, date_from, date_to)
    orders = await fetch_ozon_supply_order_details(client_id, api_key, order_ids)
    lines = []
    status_counts = {}
    for order in orders:
        order_id = order.get("supply_order_id") or order.get("order_id") or order.get("id")
        supply_number = str(order.get("supply_order_number") or order.get("order_number") or order_id)
        raw_status = order.get("status") or order.get("state") or "неизвестно"
        status_counts[raw_status] = status_counts.get(raw_status, 0) + 1

        items = await fetch_ozon_supply_order_bundle(client_id, api_key, order_id)
        for item in items:
            sku = item.get("sku")
            accepted_qty = item.get("quantity") or item.get("fact_quantity") or item.get("accepted_quantity")
            if sku is None or accepted_qty is None:
                continue
            lines.append({
                "supply_number": supply_number, "sku": sku, "accepted_qty": accepted_qty,
                "raw_json": {"order": order, "item": item, "order_status": raw_status},
            })
    return lines, status_counts


def process_supply_acceptance_line(db: "SupabaseService", line: dict) -> str:
    """Списывает сырьё + упаковку по одной строке приёмки (одна поставка + один sku).
    Возвращает 'processed' / 'skipped_unknown_sku' / 'skipped_zero_qty'."""
    sku = line["sku"]
    product_name = SKU_TO_PRODUCT_NAME.get(sku)
    if not product_name:
        return "skipped_unknown_sku"
    accepted_qty = float(line["accepted_qty"])
    if accepted_qty <= 0:
        return "skipped_zero_qty"

    today = datetime.now(TZ_MSK).date().isoformat()
    note = f"Приёмка поставки {line['supply_number']}"
    movements = [{
        "direction": "уход", "flow_type": "поставка_ozon", "product_name": product_name,
        "quantity": accepted_qty, "unit": "кг", "movement_date": today, "marketplace": "Ozon", "note": note,
    }]
    for r in db.get_product_recipe(product_name):
        movements.append({
            "direction": "уход", "flow_type": "поставка_ozon", "product_name": r["consumable_name"],
            "quantity": float(r["qty_per_unit"]) * accepted_qty, "unit": "шт",
            "movement_date": today, "marketplace": "Ozon", "note": note,
        })
    # Один атомарный INSERT: либо списываются все строки (сырьё + вся упаковка), либо ни одной.
    db.add_warehouse_movements_batch(movements)
    # Маркер дедупа пишем только ПОСЛЕ успешного списания — если insert выше упадёт, маркер не появится
    # и следующий прогон корректно повторит попытку (риск обратного случая — списание без маркера при
    # обрыве между двумя запросами — принят как временный, вернёмся к RPC-транзакции после первого
    # реального прогона).
    db.insert_supply_acceptance(
        supply_number=line["supply_number"], sku=sku, product_name=product_name,
        accepted_qty=accepted_qty, status="processed", raw_json=line.get("raw_json"),
    )
    return "processed"


async def _run_ozon_supply_acceptance_sync(db: "SupabaseService", date_from: str, date_to: str) -> dict:
    lines, status_counts = await collect_supply_acceptance_lines(OZON_BULAT_CLIENT_ID, OZON_BULAT_API_KEY, date_from, date_to)
    supply_numbers = list({l["supply_number"] for l in lines})
    existing_keys = db.get_existing_supply_acceptance_keys(supply_numbers)

    processed = skipped_dup = skipped_unknown_sku = skipped_zero_qty = errors = 0
    for line in lines:
        key = (line["supply_number"], line["sku"])
        if key in existing_keys:
            skipped_dup += 1
            continue
        try:
            result = process_supply_acceptance_line(db, line)
            if result == "processed":
                processed += 1
                existing_keys.add(key)
            elif result == "skipped_unknown_sku":
                skipped_unknown_sku += 1
            elif result == "skipped_zero_qty":
                skipped_zero_qty += 1
        except Exception:
            errors += 1
            logger.exception(f"Ошибка списания поставки {line['supply_number']} sku={line['sku']}")

    return {
        "processed": processed, "skipped_dup": skipped_dup, "skipped_unknown_sku": skipped_unknown_sku,
        "skipped_zero_qty": skipped_zero_qty, "errors": errors, "total": len(lines), "status_counts": status_counts,
    }


async def _send_supply_debug_dumps(update: Update):
    """ВРЕМЕННО: сырые ответы /v3/supply-order/list и /get отдельными сообщениями (бюджет побольше, чем в
    основном статусе) — без похода в логи Bothost. Убрать вместе с _DEBUG_LAST_SUPPLY_*_RESPONSE."""
    if _DEBUG_LAST_SUPPLY_LIST_RESPONSE is not None:
        raw = json.dumps(_DEBUG_LAST_SUPPLY_LIST_RESPONSE, ensure_ascii=False)[:3500]
        await update.message.reply_text(f"🐞 DEBUG /v3/supply-order/list сырой ответ (первые 3500 симв.):\n{raw}")
    if _DEBUG_LAST_SUPPLY_GET_RESPONSE is not None:
        raw = json.dumps(_DEBUG_LAST_SUPPLY_GET_RESPONSE, ensure_ascii=False)[:3500]
        await update.message.reply_text(f"🐞 DEBUG /v3/supply-order/get сырой ответ (первые 3500 симв.):\n{raw}")


async def _run_ozon_supply_sync_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, date_from: str, date_to: str):
    if context.bot_data.get("ozon_supply_sync_running"):
        await update.message.reply_text("⏳ Синхронизация приёмки уже идёт, подождите её завершения.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
        return
    context.bot_data["ozon_supply_sync_running"] = True
    await update.message.reply_text(f"🔄 Проверяю приёмки поставок Ozon за {date_from} — {date_to}, подождите...", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    db = context.bot_data.get("db")
    try:
        stats = await _run_ozon_supply_acceptance_sync(db, date_from, date_to)
        status_line = ", ".join(f"{k}: {v}" for k, v in stats["status_counts"].items()) or "—"
        await update.message.reply_text(
            f"✅ Приёмки за {date_from} — {date_to} (из {stats['total']} строк):\n"
            f"списано {stats['processed']}, уже было {stats['skipped_dup']}, "
            f"неизвестный SKU {stats['skipped_unknown_sku']}, нулевое кол-во {stats['skipped_zero_qty']}, "
            f"ошибок {stats['errors']}.\n\n"
            f"📋 Статусы поставок (сырые, для проверки фильтра): {status_line}",
            reply_markup=get_main_menu_keyboard(update.effective_user.id),
        )
        await _send_supply_debug_dumps(update)  # ВРЕМЕННО
    except Exception as e:
        logger.exception("Синхронизация приёмки поставок Ozon failed")
        await update.message.reply_text(f"⚠️ Ошибка синхронизации: {e}", reply_markup=get_main_menu_keyboard(update.effective_user.id))
        await _send_supply_debug_dumps(update)  # ВРЕМЕННО — вдруг запрос всё же успел дойти до ответа
    finally:
        context.bot_data["ozon_supply_sync_running"] = False


async def ozon_supply_sync_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1: выбор периода проверки приёмок (только админ)."""
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    if not (OZON_BULAT_CLIENT_ID and OZON_BULAT_API_KEY):
        await update.message.reply_text("⚠️ Ключи Ozon ещё не настроены в переменных окружения.")
        return ConversationHandler.END
    kb = [["Последние 3 дня"], ["Последние 30 дней"], ["Свой период (ДД.ММ-ДД.ММ)"], ["❌ Главное меню"]]
    await update.message.reply_text("📦 *Списание по приёмке Ozon*\n\nЗа какой период проверить поставки?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")
    return OZON_SUPPLY_SYNC_PERIOD


async def ozon_supply_sync_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    t_now = datetime.now(TZ_MSK)

    if t == "Последние 3 дня":
        date_from = (t_now.date() - timedelta(days=3)).isoformat()
        date_to = t_now.date().isoformat()
        await _run_ozon_supply_sync_and_reply(update, context, date_from, date_to)
        return ConversationHandler.END

    if t == "Последние 30 дней":
        date_from = (t_now.date() - timedelta(days=30)).isoformat()
        date_to = t_now.date().isoformat()
        await _run_ozon_supply_sync_and_reply(update, context, date_from, date_to)
        return ConversationHandler.END

    if "Свой период" in t:
        await update.message.reply_text("Введите период в формате ДД.ММ-ДД.ММ (например 01.07-31.07):", reply_markup=get_step_keyboard())
        return OZON_SUPPLY_SYNC_PERIOD_CUSTOM

    kb = [["Последние 3 дня"], ["Последние 30 дней"], ["Свой период (ДД.ММ-ДД.ММ)"], ["❌ Главное меню"]]
    await update.message.reply_text("⚠️ Не понял выбор. Нажмите один из вариантов на клавиатуре:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return OZON_SUPPLY_SYNC_PERIOD


async def ozon_supply_sync_period_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    try:
        start_raw, end_raw = t.split("-")
        start_parsed = parse_flexible_date(start_raw.strip(), TZ_MSK)
        end_parsed = parse_flexible_date(end_raw.strip(), TZ_MSK)
        if not start_parsed or not end_parsed:
            raise ValueError
        date_from, date_to = start_parsed[0], end_parsed[0]
    except (ValueError, AttributeError):
        await update.message.reply_text("⚠️ Не понял период. Формат: ДД.ММ-ДД.ММ, например 01.07-31.07. Попробуйте ещё раз:", reply_markup=get_step_keyboard())
        return OZON_SUPPLY_SYNC_PERIOD_CUSTOM

    await _run_ozon_supply_sync_and_reply(update, context, date_from, date_to)
    return ConversationHandler.END


# --- REMINDERS ---
async def reminder_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    kb = [["1. Оплатить", "2. Закупить"], ["3. Дата Поставки", "4. Дата Зачисление"], ["5. Прочее"], ["❌ Главное меню"]]
    await update.message.reply_text("⏰ *Создание Умного Напоминания*\nВыберите тему:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")
    return REMINDER_TYPE_SELECT


async def reminder_type_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t in ("❌ Главное меню", "🔙 Назад"): return await cancel_to_menu(update, context)
    context.user_data["rem_flow"] = t
    await update.message.reply_text("Введите текст напоминания (Что конкретно сделать?):", reply_markup=get_step_keyboard())
    return REMINDER_INPUT_FLOW


async def reminder_input_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t in ("❌ Главное меню", "🔙 Назад"): return await cancel_to_menu(update, context)
    context.user_data["rem_desc"] = t

    t_now = datetime.now(TZ_MSK)
    d0 = t_now.strftime("%d.%m.%Y")
    d1 = (t_now + timedelta(days=1)).strftime("%d.%m.%Y")
    kb = [[f"Сегодня ({d0})", f"Завтра ({d1})"], ["Свой вариант даты", "❌ Главное меню"]]
    await update.message.reply_text("Выберите или введите дату исполнения:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return REMINDER_DATE_SELECT


async def reminder_date_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t in ("❌ Главное меню", "🔙 Назад"): return await cancel_to_menu(update, context)
    if "Сегодня" in t or "Завтра" in t:
        context.user_data["rem_date"] = t.split("(")[1].replace(")", "").strip()
    else:
        context.user_data["rem_date"] = t

    await update.message.reply_text("Введите время в формате ЧЧ:ММ (по МСК):", reply_markup=ReplyKeyboardMarkup([["10:00", "15:00"], ["18:00"], ["❌ Главное меню"]], resize_keyboard=True))
    return REMINDER_TIME_SELECT


async def send_reminder_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    await context.bot.send_message(chat_id=job.data["uid"], text=f"⏰ *СРОЧНОЕ УВЕДОМЛЕНИЕ!*\n\nКатегория: {job.data['flow']}\n📝 Задача: {job.data['desc']}", parse_mode="Markdown")


async def reminder_time_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t in ("❌ Главное меню", "🔙 Назад"): return await cancel_to_menu(update, context)

    full_dt_str = f"{context.user_data['rem_date']} {t}"
    try:
        target_dt = datetime.strptime(full_dt_str, "%d.%m.%Y %H:%M").replace(tzinfo=TZ_MSK)
        delay = (target_dt - datetime.now(TZ_MSK)).total_seconds()
        if delay <= 0:
            await update.message.reply_text("❌ Это время уже в прошлом. Введите заново:")
            return REMINDER_TIME_SELECT
    except ValueError:
        await update.message.reply_text("❌ Формат даты не распознан. Попробуйте еще раз:")
        return REMINDER_TIME_SELECT

    db = context.bot_data.get("db")
    db.add_reminder(target_dt.isoformat(), context.user_data["rem_flow"], context.user_data["rem_desc"])

    context.job_queue.run_once(send_reminder_callback, when=delay, data={"uid": ADMIN_ID, "flow": context.user_data["rem_flow"], "desc": context.user_data["rem_desc"]})

    await update.message.reply_text(f"🚀 *Успешно принято!*\nЗадача записана в базу. Бот оповестит вас в {full_dt_str} (МСК).", reply_markup=get_main_menu_keyboard(ADMIN_ID), parse_mode="Markdown")
    return ConversationHandler.END


# --- CATALOG ADDERS ---
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["🏢 Поставщик", "📦 Товар"], ["❌ Главное меню"]]
    await update.message.reply_text("➕ Что вы хотите добавить в базу?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return ADD_SELECT


async def add_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "🏢 Поставщик":
        await update.message.reply_text("Введите телефон поставщика (или '-' если не важно):", reply_markup=get_step_keyboard())
        return ADD_SUPPLIER_PHONE
    elif t == "📦 Товар":
        await update.message.reply_text("Введите наименование нового товара:", reply_markup=get_step_keyboard())
        return ADD_PRODUCT_NAME
    return await cancel_to_menu(update, context)


async def add_supplier_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    context.user_data["new_phone"] = None if t == "-" else t
    await update.message.reply_text("Введите имя/название организации поставщика:", reply_markup=get_step_keyboard())
    return ADD_SUPPLIER_NAME


async def add_supplier_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_supplier_name"] = update.message.text.strip()
    kb = [["🌰 Сырьё", "📦 Расходники"], ["👤 Сотрудник"], ["❌ Главное меню"]]
    await update.message.reply_text("Какого типа этот контрагент?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return ADD_SUPPLIER_TYPE


async def add_supplier_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t in ("❌ Главное меню", "🔙 Назад"): return await cancel_to_menu(update, context)
    type_map = {"🌰 Сырьё": "поставщик сырья", "📦 Расходники": "поставщик расходников", "👤 Сотрудник": "сотрудник"}
    type_ = type_map.get(t, "поставщик сырья")

    db = context.bot_data.get("db")
    db.add_counterparty(context.user_data["new_supplier_name"], context.user_data.get("new_phone"), type_)
    await update.message.reply_text("✅ Поставщик добавлен в базу!", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


async def add_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_product_name"] = update.message.text.strip()
    db = context.bot_data.get("db")
    ips = db.get_my_ip_list()
    await update.message.reply_text("К какому ИП относится этот товар?", reply_markup=build_grid_keyboard(ips, columns=2))
    return ADD_PRODUCT_IP


async def add_product_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t in ("❌ Главное меню", "🔙 Назад"): return await cancel_to_menu(update, context)
    db = context.bot_data.get("db")
    db.add_product(context.user_data["new_product_name"], ip_id=db.get_ip_id(t), unit="шт")
    await update.message.reply_text("✅ Новый товар успешно занесен в перечень товаров!", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


# --- GENERAL UTILS ---
async def last_records(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = context.bot_data.get("db").get_last_operations(limit=7)
    if not rows:
        await update.message.reply_text("В базе пока нет записей.")
        return
    msg = "📋 *Последние действия в базе:*\n\n"
    for row in rows:
        cp_name = (row.get("counterparties") or {}).get("name", "—")
        msg += f"▫️ {row.get('operation_date','')} | {row.get('operation_type','')} | {cp_name} | *{row.get('amount')} ₽*\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❓ Помощь":
        await update.message.reply_text(RULES_TEXT, reply_markup=get_main_menu_keyboard(update.effective_user.id), parse_mode="Markdown")
    else:
        # Страховка: если нажали кнопку вне активного диалога (например, после перезапуска),
        # не молчим, а сразу показываем главное меню.
        await update.message.reply_text("Возврат в главное меню.", reply_markup=get_main_menu_keyboard(update.effective_user.id))


async def global_error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит ЛЮБУЮ необработанную ошибку — чтобы бот больше никогда не 'молчал' вместо ответа."""
    logger.error(f"Необработанная ошибка: {context.error}", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                f"⚠️ Произошла ошибка при обработке запроса:\n`{context.error}`\n\nПопробуйте ещё раз или вернитесь в главное меню.",
                reply_markup=get_main_menu_keyboard(update.effective_user.id),
                parse_mode="Markdown",
            )
    except Exception:
        pass
    if ADMIN_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Ошибка в боте: {context.error}")
        except Exception:
            pass


def main():
    db_service = SupabaseService()
    os.makedirs(DATA_DIR, exist_ok=True)
    application = Application.builder().token(BOT_TOKEN).build()
    application.bot_data["db"] = db_service

    supply_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📦 Закупка$"), supply_start)],
        states={
            SUPPLY_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_category)],
            SUPPLY_SUPPLIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_supplier)],
            SUPPLY_PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_product)],
            SUPPLY_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_qty)],
            SUPPLY_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_price)],
            SUPPLY_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_comment)],
            SUPPLY_ADD_MORE: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_add_more)],
            SUPPLY_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_confirm)],
        }, fallbacks=[MessageHandler(filters.Regex(r"^(❌ Главное меню|📦 Закупка|💰 Оплата|🏭 Склад|👤 Сотрудники|📜 История|📊 Баланс|➕ Добавить|❓ Помощь|⏰ Напомнить|🔄 Синхр\. Ozon|🔄 Синхр\. отзывы|📦 Приёмка Ozon)$"), cancel_to_menu)]
    )

    payment_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💰 Оплата$"), payment_start)],
        states={
            PAYMENT_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_category)],
            PAYMENT_SUPPLIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_supplier)],
            PAYMENT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_amount)],
            PAYMENT_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_type)],
            PAYMENT_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_comment)],
            PAYMENT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_confirm)],
        }, fallbacks=[MessageHandler(filters.Regex(r"^(❌ Главное меню|📦 Закупка|💰 Оплата|🏭 Склад|👤 Сотрудники|📜 История|📊 Баланс|➕ Добавить|❓ Помощь|⏰ Напомнить|🔄 Синхр\. Ozon|🔄 Синхр\. отзывы|📦 Приёмка Ozon)$"), cancel_to_menu)]
    )

    history_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📜 История$"), history_start)],
        states={
            HISTORY_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, history_category)],
            HISTORY_SUPPLIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, history_supplier)],
            HISTORY_REVERSE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, history_reverse_select)],
            HISTORY_REVERSE_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, history_reverse_number)],
            HISTORY_REVERSE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, history_reverse_confirm)],
        }, fallbacks=[MessageHandler(filters.Regex(r"^(❌ Главное меню|📦 Закупка|💰 Оплата|🏭 Склад|👤 Сотрудники|📜 История|📊 Баланс|➕ Добавить|❓ Помощь|⏰ Напомнить|🔄 Синхр\. Ozon|🔄 Синхр\. отзывы|📦 Приёмка Ozon)$"), cancel_to_menu)]
    )

    warehouse_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🏭 Склад$"), warehouse_start)],
        states={
            WAREHOUSE_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, warehouse_menu)],
            WAREHOUSE_OUT_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, warehouse_out_ip)],
            WAREHOUSE_OUT_PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, warehouse_out_product)],
            WAREHOUSE_OUT_PACKAGING: [MessageHandler(filters.TEXT & ~filters.COMMAND, warehouse_out_packaging)],
            WAREHOUSE_OUT_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, warehouse_out_qty)],
            WAREHOUSE_OUT_ADD_MORE: [MessageHandler(filters.TEXT & ~filters.COMMAND, warehouse_out_add_more)],
            WAREHOUSE_OUT_MARKETPLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, warehouse_out_marketplace)],
            WAREHOUSE_OUT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, warehouse_out_confirm)],
            WAREHOUSE_EXPENSE_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, warehouse_expense_category)],
            WAREHOUSE_EXPENSE_PAYMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, warehouse_expense_payment)],
            WAREHOUSE_EXPENSE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, warehouse_expense_amount)],
            WAREHOUSE_EXPENSE_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, warehouse_expense_comment)],
            WAREHOUSE_EXPENSE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, warehouse_expense_confirm)],
            LOGISTICS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, logistics_menu)],
            LOGISTICS_MARKETPLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, logistics_marketplace)],
            LOGISTICS_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, logistics_qty)],
            LOGISTICS_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, logistics_amount)],
            LOGISTICS_PAYMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, logistics_payment)],
            LOGISTICS_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, logistics_comment)],
            LOGISTICS_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, logistics_confirm)],
            EMPLOYEE_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, employee_menu)],
            EMP_SHIFT_EMPLOYEE: [MessageHandler(filters.TEXT & ~filters.COMMAND, emp_shift_employee)],
            EMP_SHIFT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, emp_shift_date)],
            EMP_SHIFT_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, emp_shift_start)],
            EMP_SHIFT_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, emp_shift_end)],
            EMP_SHIFT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, emp_shift_confirm)],
            EMP_SHIFT_NEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, emp_shift_next)],
            EMP_ACCRUAL_EMPLOYEE: [MessageHandler(filters.TEXT & ~filters.COMMAND, emp_accrual_employee)],
            EMP_ACCRUAL_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, emp_accrual_amount)],
            EMP_ACCRUAL_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, emp_accrual_comment)],
            EMP_ACCRUAL_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, emp_accrual_confirm)],
        }, fallbacks=[MessageHandler(filters.Regex(r"^(❌ Главное меню|📦 Закупка|💰 Оплата|🏭 Склад|👤 Сотрудники|📜 История|📊 Баланс|➕ Добавить|❓ Помощь|⏰ Напомнить|🔄 Синхр\. Ozon|🔄 Синхр\. отзывы|📦 Приёмка Ozon)$"), cancel_to_menu)]
    )

    sale_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💵 Продажа$"), sale_start)],
        states={
            SALE_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, sale_ip)],
            SALE_MARKETPLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, sale_marketplace)],
        }, fallbacks=[MessageHandler(filters.Regex(r"^(❌ Главное меню|📦 Закупка|💰 Оплата|🏭 Склад|👤 Сотрудники|📜 История|📊 Баланс|➕ Добавить|❓ Помощь|⏰ Напомнить|🔄 Синхр\. Ozon|🔄 Синхр\. отзывы|📦 Приёмка Ozon)$"), cancel_to_menu)]
    )

    balance_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📊 Баланс$"), balance_start)],
        states={
            BALANCE_MODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, balance_mode)],
            BALANCE_SUPPLIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, balance_calculate)],
        },
        fallbacks=[MessageHandler(filters.Regex(r"^(❌ Главное меню|📦 Закупка|💰 Оплата|🏭 Склад|👤 Сотрудники|📜 История|📊 Баланс|➕ Добавить|❓ Помощь|⏰ Напомнить|🔄 Синхр\. Ozon|🔄 Синхр\. отзывы|📦 Приёмка Ozon)$"), cancel_to_menu)]
    )

    reminder_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⏰ Напомнить$"), reminder_start)],
        states={
            REMINDER_TYPE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_type_select)],
            REMINDER_INPUT_FLOW: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_input_flow)],
            REMINDER_DATE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_date_select)],
            REMINDER_TIME_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_time_select)],
        }, fallbacks=[MessageHandler(filters.Regex(r"^(❌ Главное меню|📦 Закупка|💰 Оплата|🏭 Склад|👤 Сотрудники|📜 История|📊 Баланс|➕ Добавить|❓ Помощь|⏰ Напомнить|🔄 Синхр\. Ozon|🔄 Синхр\. отзывы|📦 Приёмка Ozon)$"), cancel_to_menu)]
    )

    add_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить$"), add_start)],
        states={
            ADD_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_select)],
            ADD_SUPPLIER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_supplier_phone)],
            ADD_SUPPLIER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_supplier_name)],
            ADD_SUPPLIER_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_supplier_type)],
            ADD_PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_name)],
            ADD_PRODUCT_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_ip)],
        }, fallbacks=[MessageHandler(filters.Regex(r"^(❌ Главное меню|📦 Закупка|💰 Оплата|🏭 Склад|👤 Сотрудники|📜 История|📊 Баланс|➕ Добавить|❓ Помощь|⏰ Напомнить|🔄 Синхр\. Ozon|🔄 Синхр\. отзывы|📦 Приёмка Ozon)$"), cancel_to_menu)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(approval_callback, pattern="^approve_"))
    application.add_handler(CallbackQueryHandler(feedback_action_callback, pattern="^ozfb_(pub|rej)_"))
    application.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    application.add_handler(supply_conv)
    application.add_handler(payment_conv)
    application.add_handler(history_conv)
    application.add_handler(warehouse_conv)
    application.add_handler(sale_conv)
    application.add_handler(balance_conv)
    application.add_handler(reminder_conv)
    application.add_handler(add_conv)
    application.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🔄 Синхр. Ozon$"), ozon_sync_start)],
        states={
            OZON_SYNC_PERIOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, ozon_sync_period)],
            OZON_SYNC_PERIOD_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ozon_sync_period_custom)],
        }, fallbacks=[MessageHandler(filters.Regex(r"^(❌ Главное меню|📦 Закупка|💰 Оплата|🏭 Склад|👤 Сотрудники|📜 История|📊 Баланс|➕ Добавить|❓ Помощь|⏰ Напомнить|🔄 Синхр\. Ozon|🔄 Синхр\. отзывы|📦 Приёмка Ozon)$"), cancel_to_menu)]
    ))
    application.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🔄 Синхр. отзывы$"), ozon_feedback_sync_start)],
        states={
            OZON_FEEDBACK_SYNC_PERIOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, ozon_feedback_sync_period)],
            OZON_FEEDBACK_SYNC_PERIOD_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ozon_feedback_sync_period_custom)],
        }, fallbacks=[MessageHandler(filters.Regex(r"^(❌ Главное меню|📦 Закупка|💰 Оплата|🏭 Склад|👤 Сотрудники|📜 История|📊 Баланс|➕ Добавить|❓ Помощь|⏰ Напомнить|🔄 Синхр\. Ozon|🔄 Синхр\. отзывы|📦 Приёмка Ozon)$"), cancel_to_menu)]
    ))
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(feedback_edit_start, pattern="^ozfb_edit_")],
        states={
            FEEDBACK_EDIT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_edit_text)],
        }, fallbacks=[MessageHandler(filters.Regex(r"^(❌ Главное меню|📦 Закупка|💰 Оплата|🏭 Склад|👤 Сотрудники|📜 История|📊 Баланс|➕ Добавить|❓ Помощь|⏰ Напомнить|🔄 Синхр\. Ozon|🔄 Синхр\. отзывы|📦 Приёмка Ozon)$"), cancel_to_menu)]
    ))
    application.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📦 Приёмка Ozon$"), ozon_supply_sync_start)],
        states={
            OZON_SUPPLY_SYNC_PERIOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, ozon_supply_sync_period)],
            OZON_SUPPLY_SYNC_PERIOD_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ozon_supply_sync_period_custom)],
        }, fallbacks=[MessageHandler(filters.Regex(r"^(❌ Главное меню|📦 Закупка|💰 Оплата|🏭 Склад|👤 Сотрудники|📜 История|📊 Баланс|➕ Добавить|❓ Помощь|⏰ Напомнить|🔄 Синхр\. Ozon|🔄 Синхр\. отзывы|📦 Приёмка Ozon)$"), cancel_to_menu)]
    ))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    if OZON_BULAT_CLIENT_ID and OZON_BULAT_API_KEY:
        application.job_queue.run_daily(run_ozon_sync_job, time=datetime.strptime("04:00", "%H:%M").time().replace(tzinfo=TZ_MSK))
    application.job_queue.run_daily(run_fixed_costs_job, time=datetime.strptime("05:00", "%H:%M").time().replace(tzinfo=TZ_MSK))
    if OZON_BULAT_CLIENT_ID and OZON_BULAT_API_KEY and ANTHROPIC_API_KEY:
        application.job_queue.run_repeating(run_ozon_feedback_sync_job, interval=timedelta(minutes=OZON_FEEDBACK_SYNC_MINUTES), first=60)

    application.add_error_handler(global_error_handler)

    application.run_polling()


if __name__ == "__main__":
    main()
