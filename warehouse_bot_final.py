"""
Telegram Bot for Supply & Payment Accounting
Version 7.0.0 - Migrated from Google Sheets to Supabase (Postgres)
"""
import os
import re
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
    ConversationHandler, ContextTypes, filters, CallbackQueryHandler, PicklePersistence, PersistenceInput
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
    kb = [["📦 Закупка", "💰 Оплата"], ["🏭 Склад", "👤 Сотрудники"], ["📜 История", "📊 Баланс"], ["➕ Добавить", "❓ Помощь"]]
    if user_id == ADMIN_ID:
        kb[3].append("⏰ Напомнить")
        kb.append(["🔄 Синхр. Ozon"])
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
    kb = [["📤 Фасовка/Отгрузка", "📊 Остаток на складе"], ["💸 Постоянные расходы"], ["❌ Главное меню"]]
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


# --- ПРОДАЖА (упрощённо — только реально поступившая сумма от площадки) ---
async def sale_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.bot_data.get("db")
    ips = db.get_my_ip_list()
    await update.message.reply_text("💵 *Поступление от площадки*\n\nШаг 1: Какое ИП?", reply_markup=build_grid_keyboard(ips, columns=2), parse_mode="Markdown")
    return SALE_IP


async def show_sale_marketplace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["Ozon", "WB"], ["Яндекс"], ["🔙 Назад", "❌ Главное меню"]]
    await update.message.reply_text("Шаг 2: Какая площадка?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))


async def show_sale_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Шаг 3: За какую дату/период поступление? Можно просто число (например 15) или Сегодня:",
        reply_markup=ReplyKeyboardMarkup([["Сегодня"], ["🔙 Назад", "❌ Главное меню"]], resize_keyboard=True)
    )


async def show_sale_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Шаг 4: Сколько реально поступило (₽), по отчёту площадки:", reply_markup=get_step_keyboard())


async def show_sale_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Комментарий (например «за период 1-15 июля») или '-':", reply_markup=get_step_keyboard())


async def sale_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад": return await cancel_to_menu(update, context)
    context.user_data["sale_ip"] = t
    await show_sale_marketplace(update, context)
    return SALE_MARKETPLACE


async def sale_marketplace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад": return await sale_start(update, context)
    context.user_data["sale_marketplace"] = t
    await show_sale_date(update, context)
    return SALE_PRODUCT  # переиспользуем состояние под ввод даты


async def sale_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переиспользовано под ввод даты поступления (было — выбор товара в старой версии)."""
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_sale_marketplace(update, context)
        return SALE_MARKETPLACE
    parsed = parse_flexible_date(t, TZ_MSK)
    if not parsed:
        await update.message.reply_text("⚠️ Не разобрал дату. Напишите число (15), 15.07, или Сегодня:")
        return SALE_PRODUCT
    context.user_data["sale_date_iso"], context.user_data["sale_date_display"] = parsed
    await show_sale_amount(update, context)
    return SALE_PACKAGING  # переиспользуем состояние под ввод суммы


async def sale_packaging(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переиспользовано под ввод суммы поступления (было — фасовка в старой версии)."""
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_sale_date(update, context)
        return SALE_PRODUCT
    try: context.user_data["sale_amount"] = float(t.replace(",", ".").replace(" ", ""))
    except ValueError:
        await update.message.reply_text("⚠️ Нужно ввести число. Попробуйте ещё раз:", reply_markup=get_step_keyboard())
        return SALE_PACKAGING
    await show_sale_comment(update, context)
    return SALE_UNITS  # переиспользуем состояние под комментарий


async def sale_units(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переиспользовано под комментарий (было — кол-во штук в старой версии)."""
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await show_sale_amount(update, context)
        return SALE_PACKAGING
    context.user_data["sale_comment"] = t if t != "-" else ""
    d = context.user_data
    summary = (
        f"💵 *Поступление от площадки:*\n"
        f"🏛 ИП: {d['sale_ip']}\n🛒 Площадка: {d['sale_marketplace']}\n"
        f"📅 Дата: {d['sale_date_display']}\n💰 Сумма: {d['sale_amount']} ₽"
    )
    await update.message.reply_text(summary, reply_markup=ReplyKeyboardMarkup([["✅ Подтвердить"], ["🔙 Назад", "❌ Главное меню"]], resize_keyboard=True), parse_mode="Markdown")
    return SALE_CONFIRM


async def sale_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "🔙 Назад":
        await show_sale_comment(update, context)
        return SALE_UNITS
    if t != "✅ Подтвердить": return await cancel_to_menu(update, context)
    db, d = context.bot_data.get("db"), context.user_data

    ip_id = db.get_ip_id(d["sale_ip"])
    category_id = db.get_category_id("Продажа (доход)")
    db.add_operation_returning_id(
        operation_date=d["sale_date_iso"],
        ip_id=ip_id,
        counterparty_id=None,
        category_id=category_id,
        operation_type="поступление",
        amount=d["sale_amount"],
        entered_by=str(update.effective_user.id),
        status="confirmed",
        payment_method=None,
        comment=f"{d['sale_marketplace']}: {d['sale_comment']}",
    )
    # Двойная запись: Дт Расчётный счёт / Кт Выручка — простая и честная, без оценок
    db.post_journal_entry(
        operation_id=None, entry_date=d["sale_date_iso"],
        debit_code="1010", credit_code="6000",
        amount=d["sale_amount"], comment=f"Поступление {d['sale_marketplace']}: {d['sale_comment']}",
    )
    await update.message.reply_text(f"✅ Поступление внесено: {d['sale_amount']} ₽ ({d['sale_marketplace']}).", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


# --- ПРИБЫЛЬ ---
async def profit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["💹 Прибыль за штуку", "📊 Прибыль за период"], ["❌ Главное меню"]]
    await update.message.reply_text("📈 *Прибыль*\n\nЧто показать?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")
    return PROFIT_MODE


async def profit_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t in ("❌ Главное меню", "🔙 Назад"): return await cancel_to_menu(update, context)
    db = context.bot_data.get("db")

    if t == "💹 Прибыль за штуку":
        products = db.get_all_product_names()
        await update.message.reply_text("Выберите товар:", reply_markup=build_grid_keyboard(products, columns=2))
        return PROFIT_PRODUCT

    elif t == "📊 Прибыль за период":
        t_now = datetime.now(TZ_MSK)
        this_month_start = t_now.replace(day=1).strftime("%d.%m")
        kb = [[f"Этот месяц (с {this_month_start})"], ["Свой период (начало-конец)"], ["❌ Главное меню"]]
        await update.message.reply_text("За какой период?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return PROFIT_PERIOD

    return PROFIT_MODE


async def profit_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад": return await profit_start(update, context)
    context.user_data["profit_product"] = t
    await update.message.reply_text("Фасовка (например «250г»):", reply_markup=get_step_keyboard())
    return PROFIT_PACKAGING


async def profit_packaging(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        products = context.bot_data.get("db").get_all_product_names()
        await update.message.reply_text("Выберите товар:", reply_markup=build_grid_keyboard(products, columns=2))
        return PROFIT_PRODUCT

    grams = parse_packaging_grams(t)
    if not grams:
        await update.message.reply_text("⚠️ Не понял фасовку. Введите, например, «250г».", reply_markup=get_step_keyboard())
        return PROFIT_PACKAGING

    context.user_data["profit_packaging"] = t
    kb = [["Ozon", "WB"], ["Яндекс"], ["🔙 Назад", "❌ Главное меню"]]
    await update.message.reply_text("Для какой площадки считаем (логистика разная)?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return PROFIT_MARKETPLACE


async def profit_marketplace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "🔙 Назад":
        await update.message.reply_text("Фасовка (например «250г»):", reply_markup=get_step_keyboard())
        return PROFIT_PACKAGING

    db = context.bot_data.get("db")
    product = context.user_data["profit_product"]
    packaging = context.user_data["profit_packaging"]
    grams = parse_packaging_grams(packaging)

    cost_per_kg = db.get_material_cost_per_kg(product)
    if not cost_per_kg:
        await update.message.reply_text(f"⚠️ Нет данных о закупках «{product}» с известной ценой — себестоимость сырья посчитать нельзя.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
        return ConversationHandler.END

    comp = db.get_unit_cost_components(product, packaging) or {}
    labor = float(comp.get("labor") or LABOR_COST_PER_UNIT)
    packaging_cost = float(comp.get("packaging_cost") or 0)
    rent = float(comp.get("rent") or 0)
    logistics_map = {"Ozon": comp.get("logistics_ozon"), "WB": comp.get("logistics_wb"), "Яндекс": comp.get("logistics_yandex")}
    logistics = logistics_map.get(t)
    logistics = float(logistics) if logistics is not None else 0.0

    material_cost = round(cost_per_kg * grams / 1000, 2)
    total_cost = round(material_cost + labor + packaging_cost + rent + logistics, 2)

    text = (
        f"💹 *Полная себестоимость: {product} ({packaging}) — {t}*\n\n"
        f"🌰 Сырьё ({cost_per_kg} ₽/кг × {grams}г): {material_cost} ₽\n"
        f"👤 Труд: {labor} ₽\n"
        f"📦 Расходники: {packaging_cost} ₽\n"
        f"🏭 Аренда: {rent} ₽\n"
        f"🚚 Логистика ({t}): {logistics} ₽\n"
        f"💰 *Итого себестоимость 1 шт: {total_cost} ₽*\n\n"
        f"_Чтобы узнать прибыль — сравните эту цифру с реальной ценой продажи на {t}._"
    )
    await update.message.reply_text(text, reply_markup=get_main_menu_keyboard(update.effective_user.id), parse_mode="Markdown")
    return ConversationHandler.END


async def profit_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    db = context.bot_data.get("db")
    t_now = datetime.now(TZ_MSK)

    if "Этот месяц" in t:
        date_from = t_now.replace(day=1).date().isoformat()
        date_to = t_now.date().isoformat()
    else:
        await update.message.reply_text("Введите период в формате ДД.ММ-ДД.ММ (например 01.07-18.07):", reply_markup=get_step_keyboard())
        return PROFIT_PERIOD_CUSTOM

    pnl = db.get_period_pnl(date_from, date_to)
    await update.message.reply_text(format_pnl_text(pnl, date_from, date_to), reply_markup=get_main_menu_keyboard(update.effective_user.id), parse_mode="Markdown")
    return ConversationHandler.END


async def profit_period_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    db = context.bot_data.get("db")
    t_now = datetime.now(TZ_MSK)

    try:
        start_raw, end_raw = t.split("-")
        start_parsed = parse_flexible_date(start_raw.strip(), TZ_MSK)
        end_parsed = parse_flexible_date(end_raw.strip(), TZ_MSK)
        if not start_parsed or not end_parsed:
            raise ValueError
        date_from, date_to = start_parsed[0], end_parsed[0]
    except (ValueError, AttributeError):
        await update.message.reply_text("⚠️ Не понял период. Формат: ДД.ММ-ДД.ММ. Попробуйте ещё раз:", reply_markup=get_step_keyboard())
        return PROFIT_PERIOD_CUSTOM

    pnl = db.get_period_pnl(date_from, date_to)
    await update.message.reply_text(format_pnl_text(pnl, date_from, date_to), reply_markup=get_main_menu_keyboard(update.effective_user.id), parse_mode="Markdown")
    return ConversationHandler.END


def format_pnl_text(pnl: dict, date_from: str, date_to: str) -> str:
    lines = [f"📊 *Прибыль за период {date_from} — {date_to}*\n", f"💰 Выручка: *{pnl['revenue']} ₽*\n", "📉 *Расходы по категориям:*"]
    for name, amt in pnl["expenses_by_category"]:
        lines.append(f"▫️ {name}: {round(amt,2)} ₽")
    lines.append(f"\n📉 Всего расходов: *{pnl['total_expenses']} ₽*")
    profit_word = "Прибыль" if pnl["net_profit"] >= 0 else "Убыток"
    lines.append(f"\n💰 *{profit_word}: {pnl['net_profit']} ₽*")
    return "\n".join(lines)


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
    count = 0
    for op in ops:
        posting = op.get("posting") or {}
        items = op.get("items") or []
        item_name = items[0].get("name") if items else None
        sku = items[0].get("sku") if items else None
        db.upsert_ozon_transaction(
            ozon_operation_id=op.get("operation_id"),
            ip_id=ip_id,
            operation_date=(op.get("operation_date") or "")[:10] or date_from,
            operation_type=op.get("operation_type"),
            operation_type_name=op.get("operation_type_name"),
            posting_number=posting.get("posting_number"),
            sku=sku,
            item_name=item_name,
            amount=op.get("amount", 0),
            accruals_for_sale=op.get("accruals_for_sale"),
            commission_amount=op.get("sale_commission"),
            delivery_charge=(op.get("delivery_charge") or {}).get("amount"),
            return_delivery_charge=(op.get("return_delivery_charge") or {}).get("amount"),
            raw_json=op,
        )
        count += 1
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


async def ozon_sync_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной запуск синхронизации по кнопке (только админ)."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not OZON_BULAT_CLIENT_ID or not OZON_BULAT_API_KEY:
        await update.message.reply_text("⚠️ Ключи Ozon ещё не настроены в переменных окружения.")
        return
    if context.bot_data.get("ozon_sync_running"):
        await update.message.reply_text("⏳ Синхронизация уже идёт, подождите её завершения — не нажимайте повторно.")
        return
    context.bot_data["ozon_sync_running"] = True
    await update.message.reply_text("🔄 Синхронизирую данные с Ozon за последние 30 дней, подождите...")
    db = context.bot_data.get("db")
    date_to = datetime.now(TZ_MSK).date().isoformat()
    date_from = (datetime.now(TZ_MSK).date() - timedelta(days=30)).isoformat()
    try:
        count = await sync_ozon_transactions(db, "Булат", OZON_BULAT_CLIENT_ID, OZON_BULAT_API_KEY, date_from, date_to)
        await update.message.reply_text(f"✅ Готово: синхронизировано {count} транзакций за {date_from} — {date_to}.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка синхронизации: {e}", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    finally:
        context.bot_data["ozon_sync_running"] = False


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


def main():
    db_service = SupabaseService()
    os.makedirs(DATA_DIR, exist_ok=True)
    persistence = PicklePersistence(
        filepath=os.path.join(DATA_DIR, "bot_persistence_v4.pickle"),
        store_data=PersistenceInput(bot_data=False, chat_data=True, user_data=True, callback_data=True),
    )
    application = Application.builder().token(BOT_TOKEN).persistence(persistence).build()
    application.bot_data["db"] = db_service

    supply_conv = ConversationHandler(
        name="supply_conv",
        persistent=True,
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
        }, fallbacks=[MessageHandler(filters.Regex("^❌ Главное меню$"), cancel_to_menu)]
    )

    payment_conv = ConversationHandler(
        name="payment_conv",
        persistent=True,
        entry_points=[MessageHandler(filters.Regex("^💰 Оплата$"), payment_start)],
        states={
            PAYMENT_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_category)],
            PAYMENT_SUPPLIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_supplier)],
            PAYMENT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_amount)],
            PAYMENT_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_type)],
            PAYMENT_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_comment)],
            PAYMENT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_confirm)],
        }, fallbacks=[MessageHandler(filters.Regex("^❌ Главное меню$"), cancel_to_menu)]
    )

    history_conv = ConversationHandler(
        name="history_conv",
        persistent=True,
        entry_points=[MessageHandler(filters.Regex("^📜 История$"), history_start)],
        states={
            HISTORY_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, history_category)],
            HISTORY_SUPPLIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, history_supplier)],
            HISTORY_REVERSE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, history_reverse_select)],
            HISTORY_REVERSE_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, history_reverse_number)],
            HISTORY_REVERSE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, history_reverse_confirm)],
        }, fallbacks=[MessageHandler(filters.Regex("^❌ Главное меню$"), cancel_to_menu)]
    )

    warehouse_conv = ConversationHandler(
        name="warehouse_conv",
        persistent=True,
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
        }, fallbacks=[MessageHandler(filters.Regex("^❌ Главное меню$"), cancel_to_menu)]
    )

    employee_conv = ConversationHandler(
        name="employee_conv",
        persistent=True,
        entry_points=[MessageHandler(filters.Regex("^👤 Сотрудники$"), employee_start)],
        states={
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
        }, fallbacks=[MessageHandler(filters.Regex("^❌ Главное меню$"), cancel_to_menu)]
    )

    sale_conv = ConversationHandler(
        name="sale_conv",
        persistent=True,
        entry_points=[MessageHandler(filters.Regex("^📥 Поступление$"), sale_start)],
        states={
            SALE_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, sale_ip)],
            SALE_MARKETPLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, sale_marketplace)],
            SALE_PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, sale_product)],
            SALE_PACKAGING: [MessageHandler(filters.TEXT & ~filters.COMMAND, sale_packaging)],
            SALE_UNITS: [MessageHandler(filters.TEXT & ~filters.COMMAND, sale_units)],
            SALE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, sale_confirm)],
        }, fallbacks=[MessageHandler(filters.Regex("^❌ Главное меню$"), cancel_to_menu)]
    )

    profit_conv = ConversationHandler(
        name="profit_conv",
        persistent=True,
        entry_points=[MessageHandler(filters.Regex("^📈 Прибыль$"), profit_start)],
        states={
            PROFIT_MODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, profit_mode)],
            PROFIT_PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, profit_product)],
            PROFIT_PACKAGING: [MessageHandler(filters.TEXT & ~filters.COMMAND, profit_packaging)],
            PROFIT_MARKETPLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, profit_marketplace)],
            PROFIT_PERIOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, profit_period)],
            PROFIT_PERIOD_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, profit_period_custom)],
        }, fallbacks=[MessageHandler(filters.Regex("^❌ Главное меню$"), cancel_to_menu)]
    )

    balance_conv = ConversationHandler(
        name="balance_conv",
        persistent=True,
        entry_points=[MessageHandler(filters.Regex("^📊 Баланс$"), balance_start)],
        states={
            BALANCE_MODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, balance_mode)],
            BALANCE_SUPPLIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, balance_calculate)],
        },
        fallbacks=[MessageHandler(filters.Regex("^❌ Главное меню$"), cancel_to_menu)]
    )

    reminder_conv = ConversationHandler(
        name="reminder_conv",
        persistent=True,
        entry_points=[MessageHandler(filters.Regex("^⏰ Напомнить$"), reminder_start)],
        states={
            REMINDER_TYPE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_type_select)],
            REMINDER_INPUT_FLOW: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_input_flow)],
            REMINDER_DATE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_date_select)],
            REMINDER_TIME_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_time_select)],
        }, fallbacks=[MessageHandler(filters.Regex("^❌ Главное меню$"), cancel_to_menu)]
    )

    add_conv = ConversationHandler(
        name="add_conv",
        persistent=True,
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить$"), add_start)],
        states={
            ADD_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_select)],
            ADD_SUPPLIER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_supplier_phone)],
            ADD_SUPPLIER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_supplier_name)],
            ADD_SUPPLIER_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_supplier_type)],
            ADD_PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_name)],
            ADD_PRODUCT_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_ip)],
        }, fallbacks=[MessageHandler(filters.Regex("^❌ Главное меню$"), cancel_to_menu)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(approval_callback, pattern="^approve_"))
    application.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    application.add_handler(supply_conv)
    application.add_handler(payment_conv)
    application.add_handler(history_conv)
    application.add_handler(warehouse_conv)
    application.add_handler(employee_conv)
    # sale_conv и profit_conv временно отключены — раздел "Продажи/Прибыль" будет доработан отдельно
    # application.add_handler(sale_conv)
    # application.add_handler(profit_conv)
    application.add_handler(balance_conv)
    application.add_handler(reminder_conv)
    application.add_handler(add_conv)
    application.add_handler(MessageHandler(filters.Regex("^🔄 Синхр. Ozon$"), ozon_sync_manual))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    if OZON_BULAT_CLIENT_ID and OZON_BULAT_API_KEY:
        application.job_queue.run_daily(run_ozon_sync_job, time=datetime.strptime("04:00", "%H:%M").time().replace(tzinfo=TZ_MSK))

    application.run_polling()


if __name__ == "__main__":
    main()
