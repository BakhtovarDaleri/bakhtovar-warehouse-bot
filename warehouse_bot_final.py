"""
Telegram Bot for Supply & Payment Accounting
Version 7.0.0 - Migrated from Google Sheets to Supabase (Postgres)
"""
import os
import re
import logging
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
        self._load_caches()

    def _load_caches(self):
        cats = self.client.table("categories").select("id,name").execute().data or []
        self._category_cache = {c["name"]: c["id"] for c in cats}
        ips = self.client.table("ip").select("id,name").execute().data or []
        self._ip_cache = {i["name"]: i["id"] for i in ips}

    def refresh_ip_cache(self):
        self._load_caches()

    def get_category_id(self, name: str):
        return self._category_cache.get(name)

    def get_ip_id(self, name: str):
        return self._ip_cache.get(name)

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
(
    SUPPLY_CATEGORY, SUPPLY_SUPPLIER, SUPPLY_MY_IP, SUPPLY_PRODUCT, SUPPLY_QTY, SUPPLY_PRICE, SUPPLY_UNIT, SUPPLY_COMMENT, SUPPLY_CONFIRM, SUPPLY_ADD_MORE,
    PAYMENT_CATEGORY, PAYMENT_SUPPLIER, PAYMENT_AMOUNT, PAYMENT_TYPE, PAYMENT_COMMENT, PAYMENT_CONFIRM,
    ADD_SELECT, ADD_SUPPLIER_PHONE, ADD_SUPPLIER_NAME, ADD_SUPPLIER_TYPE, ADD_SUPPLIER_CONFIRM,
    ADD_MY_IP_NAME, ADD_MY_IP_CONFIRM, ADD_PRODUCT_NAME, ADD_PRODUCT_IP,
    REMINDER_TYPE_SELECT, REMINDER_INPUT_FLOW, REMINDER_DATE_SELECT, REMINDER_TIME_SELECT,
    BALANCE_SUPPLIER, BALANCE_MODE,
    HISTORY_CATEGORY, HISTORY_SUPPLIER, HISTORY_REVERSE_SELECT, HISTORY_REVERSE_NUMBER, HISTORY_REVERSE_CONFIRM,
    WAREHOUSE_MENU,
    WAREHOUSE_IN_SUPPLIER, WAREHOUSE_IN_PRODUCT, WAREHOUSE_IN_QTY, WAREHOUSE_IN_COMMENT, WAREHOUSE_IN_CONFIRM,
    WAREHOUSE_OUT_IP, WAREHOUSE_OUT_PRODUCT, WAREHOUSE_OUT_PACKAGING, WAREHOUSE_OUT_QTY, WAREHOUSE_OUT_ADD_MORE, WAREHOUSE_OUT_MARKETPLACE, WAREHOUSE_OUT_CONFIRM,
    EMPLOYEE_MENU,
    EMP_SHIFT_EMPLOYEE, EMP_SHIFT_DATE, EMP_SHIFT_START, EMP_SHIFT_END, EMP_SHIFT_CONFIRM, EMP_SHIFT_NEXT,
    EMP_ACCRUAL_EMPLOYEE, EMP_ACCRUAL_AMOUNT, EMP_ACCRUAL_COMMENT, EMP_ACCRUAL_CONFIRM
) = range(60)


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
    kb = [["📦 Закупка", "💰 Оплата"], ["📜 История", "📊 Баланс"], ["🏭 Склад", "👤 Сотрудники"], ["➕ Добавить", "❓ Помощь"]]
    if user_id == ADMIN_ID:
        kb[3].append("⏰ Напомнить")
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
        db.add_operation(
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
            movement_date=op_date,
            counterparty_id=cp_id,
            ip_id=None,
            marketplace=None,
            note=d["s_comment"],
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

    db.add_operation(
        operation_date=datetime.now(TZ_MSK).date().isoformat(),
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
    await update.message.reply_text("✅ Оплата успешно сохранена в систему!", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


# --- BALANCE LOGIC ---
async def balance_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["🔍 Один контрагент", "📋 Все долги"], ["❌ Главное меню"]]
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

    reversal_id = db.add_operation_returning_id(
        operation_date=datetime.now(TZ_MSK).date().isoformat(),
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

    await update.message.reply_text(
        f"✅ Операция отменена. Создана компенсирующая запись на {-float(row['amount'])} ₽.\n"
        f"Исходная запись осталась в истории (не удалена).",
        reply_markup=get_main_menu_keyboard(update.effective_user.id)
    )
    return ConversationHandler.END


# --- WAREHOUSE (СКЛАД) ---
async def warehouse_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["📤 Фасовка/Отгрузка", "📊 Остаток на складе"], ["❌ Главное меню"]]
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

    return WAREHOUSE_MENU


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
    db.add_operation(
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
    db.add_operation(
        operation_date=datetime.now(TZ_MSK).date().isoformat(),
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
    await update.message.reply_text(f"✅ Начислено {d['emp_amount']} ₽ для {d['emp_name']}.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
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


def main():
    db_service = SupabaseService()
    os.makedirs(DATA_DIR, exist_ok=True)
    persistence = PicklePersistence(
        filepath=os.path.join(DATA_DIR, "bot_persistence.pickle"),
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
    application.add_handler(balance_conv)
    application.add_handler(reminder_conv)
    application.add_handler(add_conv)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.run_polling()


if __name__ == "__main__":
    main()
