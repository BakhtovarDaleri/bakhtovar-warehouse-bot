"""
Telegram Bot for Supply & Payment Accounting
Version 7.0.0 - Migrated from Google Sheets to Supabase (Postgres)
"""
import os
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
    ConversationHandler, ContextTypes, filters, CallbackQueryHandler
)
from supabase import create_client, Client

# --- CONFIGURATION ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")  # service_role key — не публиковать нигде

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
    SUPPLY_CATEGORY, SUPPLY_SUPPLIER, SUPPLY_MY_IP, SUPPLY_PRODUCT, SUPPLY_QTY, SUPPLY_PRICE, SUPPLY_UNIT, SUPPLY_COMMENT, SUPPLY_CONFIRM,
    PAYMENT_CATEGORY, PAYMENT_SUPPLIER, PAYMENT_AMOUNT, PAYMENT_TYPE, PAYMENT_COMMENT, PAYMENT_CONFIRM,
    ADD_SELECT, ADD_SUPPLIER_PHONE, ADD_SUPPLIER_NAME, ADD_SUPPLIER_TYPE, ADD_SUPPLIER_CONFIRM,
    ADD_MY_IP_NAME, ADD_MY_IP_CONFIRM, ADD_PRODUCT_NAME, ADD_PRODUCT_IP,
    REMINDER_TYPE_SELECT, REMINDER_INPUT_FLOW, REMINDER_DATE_SELECT, REMINDER_TIME_SELECT,
    BALANCE_SUPPLIER,
    HISTORY_CATEGORY, HISTORY_SUPPLIER
) = range(31)


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
    kb = [["📦 Закупка", "💰 Оплата"], ["📜 История", "📊 Баланс"], ["📋 Последние записи", "➕ Добавить"], ["❓ Помощь"]]
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


# --- SUPPLY PROCESS ---
async def supply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [TYPE_BUTTONS_SUPPLY, ["❌ Главное меню"]]
    await update.message.reply_text("📦 *Новая закупка*\n\nШаг 1: Что закупаем?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")
    return SUPPLY_CATEGORY


async def supply_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t not in TYPE_BUTTON_TO_DB: return SUPPLY_CATEGORY
    context.user_data["s_type_label"] = t
    context.user_data["s_cp_type"] = TYPE_BUTTON_TO_DB[t]

    db = context.bot_data.get("db")
    sups = [s["name"] for s in db.get_suppliers_list(type_filter=context.user_data["s_cp_type"])]
    await update.message.reply_text("Шаг 2: Выберите поставщика:", reply_markup=build_grid_keyboard(sups, columns=2))
    return SUPPLY_SUPPLIER


async def supply_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    context.user_data["s_supplier"] = t

    db = context.bot_data.get("db")
    cp = db.get_counterparty_by_name(t)
    context.user_data["s_phone"] = (cp or {}).get("phone", "")

    ips = db.get_my_ip_list()
    await update.message.reply_text("Шаг 3: Выберите наше ИП:", reply_markup=build_grid_keyboard(ips, columns=2))
    return SUPPLY_MY_IP


async def supply_my_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    context.user_data["s_my_ip"] = t

    db = context.bot_data.get("db")
    if context.user_data["s_cp_type"] == "поставщик сырья":
        # Сырьё — товары, привязанные именно к выбранному ИП
        items = db.get_products_list(ip_name=t)
    else:
        # Расходники — единый каталог вид+размер, без привязки к ИП
        items = db.get_consumables_list()

    await update.message.reply_text("Шаг 4: Выберите наименование:", reply_markup=build_grid_keyboard(items, columns=2))
    return SUPPLY_PRODUCT


async def supply_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    context.user_data["s_product"] = t
    await update.message.reply_text("Шаг 5: Введите Количество (число):", reply_markup=get_step_keyboard())
    return SUPPLY_QTY


async def supply_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    try: context.user_data["s_qty"] = float(t.replace(",", ".").replace(" ", ""))
    except ValueError: return SUPPLY_QTY
    await update.message.reply_text("Шаг 6: Введите Цену за единицу:", reply_markup=get_step_keyboard())
    return SUPPLY_PRICE


async def supply_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    try: context.user_data["s_price"] = float(t.replace(",", ".").replace(" ", ""))
    except ValueError: return SUPPLY_PRICE
    await update.message.reply_text("Шаг 7: Выберите единицу измерения:", reply_markup=ReplyKeyboardMarkup([["кг", "шт"], ["❌ Главное меню"]], resize_keyboard=True))
    return SUPPLY_UNIT


async def supply_unit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    context.user_data["s_unit"] = t
    context.user_data["s_total"] = round(context.user_data["s_qty"] * context.user_data["s_price"], 2)
    await update.message.reply_text("Добавьте комментарий к закупке (или нажмите '-'):", reply_markup=get_step_keyboard())
    return SUPPLY_COMMENT


async def supply_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    context.user_data["s_comment"] = t if t != "-" else ""
    d = context.user_data
    summary = f"📋 *Проверка закупки:*\n🏢 Поставщик: {d['s_supplier']}\n📦 Товар: {d['s_product']}\n🔢 Кол-во: {d['s_qty']} {d['s_unit']}\n💰 Сумма: {d['s_total']} ₽"
    await update.message.reply_text(summary, reply_markup=ReplyKeyboardMarkup([["✅ Подтвердить", "❌ Главное меню"]], resize_keyboard=True), parse_mode="Markdown")
    return SUPPLY_CONFIRM


async def supply_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() != "✅ Подтвердить": return await cancel_to_menu(update, context)
    db, d = context.bot_data.get("db"), context.user_data

    cp = db.get_counterparty_by_name(d["s_supplier"])
    category_name = infer_category_name(d.get("s_cp_type", ""))

    db.add_operation(
        operation_date=datetime.now(TZ_MSK).date().isoformat(),
        ip_id=db.get_ip_id(d["s_my_ip"]),
        counterparty_id=(cp or {}).get("id"),
        category_id=db.get_category_id(category_name),
        operation_type="покупка",
        amount=d["s_total"],
        quantity=d["s_qty"],
        price=d["s_price"],
        item_name=d["s_product"],
        entered_by=str(update.effective_user.id),
        status="confirmed",
        payment_method=None,
        comment=d["s_comment"],
    )
    await update.message.reply_text("✅ Данные закупки успешно занесены в базу!", reply_markup=get_main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


# --- PAYMENT PROCESS ---
async def payment_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [TYPE_BUTTONS_FULL, ["❌ Главное меню"]]
    await update.message.reply_text("💰 *Внесение оплаты*\n\nШаг 1: Кому платим?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")
    return PAYMENT_CATEGORY


async def payment_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t not in TYPE_BUTTON_TO_DB: return PAYMENT_CATEGORY
    context.user_data["p_cp_type"] = TYPE_BUTTON_TO_DB[t]

    db = context.bot_data.get("db")
    sups = [s["name"] for s in db.get_suppliers_list(type_filter=context.user_data["p_cp_type"])]
    await update.message.reply_text("Шаг 2: Выберите получателя:", reply_markup=build_grid_keyboard(sups, columns=2))
    return PAYMENT_SUPPLIER


async def payment_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    context.user_data["p_supplier"] = t

    db = context.bot_data.get("db")
    cp = db.get_counterparty_by_name(t)
    context.user_data["p_phone"] = (cp or {}).get("phone", "")

    debt = db.get_supplier_current_debt(t)
    kb = [[f"{debt}"], ["Ввести сумму вручную"], ["❌ Главное меню"]]
    await update.message.reply_text(f"Шаг 3: Текущий долг перед получателем: *{debt} ₽*.\nВыберите готовую сумму долга или нажмите ввод вручную:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")
    return PAYMENT_AMOUNT


async def payment_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t == "Ввести сумму вручную":
        await update.message.reply_text("Введите точную сумму платежа цифрами:", reply_markup=get_step_keyboard())
        return PAYMENT_AMOUNT

    try: context.user_data["p_amount"] = float(t.replace(",", ".").replace(" ", ""))
    except ValueError: return PAYMENT_AMOUNT

    await update.message.reply_text("Шаг 3: Выберите тип счёта оплаты:", reply_markup=ReplyKeyboardMarkup([["Наличные", "Карта ВТБ"], ["Безнал ИП"], ["❌ Главное меню"]], resize_keyboard=True))
    return PAYMENT_TYPE


async def payment_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    context.user_data["p_type"] = t
    await update.message.reply_text("Добавьте комментарий (или '-'):", reply_markup=get_step_keyboard())
    return PAYMENT_COMMENT


async def payment_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    context.user_data["p_comment"] = t if t != "-" else ""
    d = context.user_data
    await update.message.reply_text(f"💰 *Платёж:*\n🏢 Получатель: {d['p_supplier']}\n💰 Сумма: {d['p_amount']} ₽\n🏦 Счёт: {d['p_type']}", reply_markup=ReplyKeyboardMarkup([["✅ Подтвердить", "❌ Главное меню"]], resize_keyboard=True), parse_mode="Markdown")
    return PAYMENT_CONFIRM


async def payment_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() != "✅ Подтвердить": return await cancel_to_menu(update, context)
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
    sups = [s["name"] for s in context.bot_data.get("db").get_suppliers_list()]
    await update.message.reply_text("📊 Выберите контрагента для вывода точного баланса:", reply_markup=build_grid_keyboard(sups, columns=2))
    return BALANCE_SUPPLIER


async def balance_calculate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    debt = context.bot_data.get("db").get_supplier_current_debt(t)
    await update.message.reply_text(f"📊 Контрагент: `{t}`\n💰 Текущий баланс долга: *{debt}* ₽", reply_markup=get_main_menu_keyboard(update.effective_user.id), parse_mode="Markdown")
    return ConversationHandler.END


# --- HISTORY BY SUPPLIER ---
async def history_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [TYPE_BUTTONS_FULL, ["❌ Главное меню"]]
    await update.message.reply_text("📜 *История по контрагенту*\n\nШаг 1: Какой тип?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")
    return HISTORY_CATEGORY


async def history_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    if t not in TYPE_BUTTON_TO_DB: return HISTORY_CATEGORY
    db = context.bot_data.get("db")
    sups = [s["name"] for s in db.get_suppliers_list(type_filter=TYPE_BUTTON_TO_DB[t])]
    await update.message.reply_text("Шаг 2: Выберите контрагента:", reply_markup=build_grid_keyboard(sups, columns=2))
    return HISTORY_SUPPLIER


async def history_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    db = context.bot_data.get("db")
    cp = db.get_counterparty_by_name(t)
    if not cp:
        await update.message.reply_text("Контрагент не найден.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
        return ConversationHandler.END

    rows = db.get_history(cp["id"])
    if not rows:
        await update.message.reply_text(f"📜 История по «{t}» пуста.", reply_markup=get_main_menu_keyboard(update.effective_user.id))
        return ConversationHandler.END

    running = 0.0
    lines = [f"📜 *История: {t}*\n"]
    for r in rows:
        running += float(r["amount"])
        sign = "➕" if float(r["amount"]) >= 0 else "➖"
        item = r.get("item_name") or ""
        lines.append(f"{sign} {r.get('operation_date','')} | {r.get('operation_type','')} {item} | {r['amount']} ₽ | итог: {round(running,2)} ₽")

    text = "\n".join(lines[:1] + lines[-20:]) if len(lines) > 21 else "\n".join(lines)
    text += f"\n\n💰 *Текущий остаток долга: {round(running,2)} ₽*"
    await update.message.reply_text(text, reply_markup=get_main_menu_keyboard(update.effective_user.id), parse_mode="Markdown")
    return ConversationHandler.END


# --- REMINDERS ---
async def reminder_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    kb = [["1. Оплатить", "2. Закупить"], ["3. Дата Поставки", "4. Дата Зачисление"], ["5. Прочее"], ["❌ Главное меню"]]
    await update.message.reply_text("⏰ *Создание Умного Напоминания*\nВыберите тему:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True), parse_mode="Markdown")
    return REMINDER_TYPE_SELECT


async def reminder_type_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    context.user_data["rem_flow"] = t
    await update.message.reply_text("Введите текст напоминания (Что конкретно сделать?):", reply_markup=get_step_keyboard())
    return REMINDER_INPUT_FLOW


async def reminder_input_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
    context.user_data["rem_desc"] = t

    t_now = datetime.now(TZ_MSK)
    d0 = t_now.strftime("%d.%m.%Y")
    d1 = (t_now + timedelta(days=1)).strftime("%d.%m.%Y")
    kb = [[f"Сегодня ({d0})", f"Завтра ({d1})"], ["Свой вариант даты", "❌ Главное меню"]]
    await update.message.reply_text("Выберите или введите дату исполнения:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return REMINDER_DATE_SELECT


async def reminder_date_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
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
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)

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
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
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
    if t == "❌ Главное меню": return await cancel_to_menu(update, context)
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
    if t == "📋 Последние записи": await last_records(update, context)
    elif t == "❓ Помощь": await update.message.reply_text(RULES_TEXT, reply_markup=get_main_menu_keyboard(update.effective_user.id), parse_mode="Markdown")


def main():
    db_service = SupabaseService()
    application = Application.builder().token(BOT_TOKEN).build()
    application.bot_data["db"] = db_service

    supply_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📦 Закупка$"), supply_start)],
        states={
            SUPPLY_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_category)],
            SUPPLY_SUPPLIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_supplier)],
            SUPPLY_MY_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_my_ip)],
            SUPPLY_PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_product)],
            SUPPLY_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_qty)],
            SUPPLY_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_price)],
            SUPPLY_UNIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_unit)],
            SUPPLY_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_comment)],
            SUPPLY_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_confirm)],
        }, fallbacks=[MessageHandler(filters.Regex("^❌ Главное меню$"), cancel_to_menu)]
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
        }, fallbacks=[MessageHandler(filters.Regex("^❌ Главное меню$"), cancel_to_menu)]
    )

    history_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📜 История$"), history_start)],
        states={
            HISTORY_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, history_category)],
            HISTORY_SUPPLIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, history_supplier)],
        }, fallbacks=[MessageHandler(filters.Regex("^❌ Главное меню$"), cancel_to_menu)]
    )

    balance_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📊 Баланс$"), balance_start)],
        states={BALANCE_SUPPLIER: [MessageHandler(filters.TEXT & ~filters.COMMAND, balance_calculate)]},
        fallbacks=[MessageHandler(filters.Regex("^❌ Главное меню$"), cancel_to_menu)]
    )

    reminder_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⏰ Напомнить$"), reminder_start)],
        states={
            REMINDER_TYPE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_type_select)],
            REMINDER_INPUT_FLOW: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_input_flow)],
            REMINDER_DATE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_date_select)],
            REMINDER_TIME_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_time_select)],
        }, fallbacks=[MessageHandler(filters.Regex("^❌ Главное меню$"), cancel_to_menu)]
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
        }, fallbacks=[MessageHandler(filters.Regex("^❌ Главное меню$"), cancel_to_menu)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(approval_callback, pattern="^approve_"))
    application.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    application.add_handler(supply_conv)
    application.add_handler(payment_conv)
    application.add_handler(history_conv)
    application.add_handler(balance_conv)
    application.add_handler(reminder_conv)
    application.add_handler(add_conv)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.run_polling()


if __name__ == "__main__":
    main()
