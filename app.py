import os
import re
import html
import hashlib
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0") or 0)
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://asmar-robert-bot.onrender.com"
).rstrip("/")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest() if BOT_TOKEN else ""

# -----------------------------------------------------------------------------
# UI prototype state only.
# This is intentionally temporary/in-memory for the first interface test.
# Persistent DB + real iChancy bridge will be added after UI approval.
# -----------------------------------------------------------------------------
users = {}
flows = {}
panel_message_ids = {}

DEFAULT_BONUSES = {
    "sham": 0,
    "syriatel": 0,
    "usdt": 0,
    "wish": 0,
}


def tg(method, payload=None, timeout=15):
    if not BOT_TOKEN:
        return {"ok": False, "description": "BOT_TOKEN missing"}
    try:
        r = requests.post(f"{TG_API}/{method}", json=payload or {}, timeout=timeout)
        data = r.json()
        if not data.get("ok"):
            print(f"Telegram API error in {method}: {data}", flush=True)
        return data
    except Exception as exc:
        print(f"Telegram API exception in {method}: {exc}", flush=True)
        return {"ok": False}


def ensure_native_menu():
    """Enable Telegram's native bottom-left Menu button.

    The native menu contains one ready command only: /start — START.
    """
    commands = [
        {"command": "start", "description": "START"},
    ]
    tg("setMyCommands", {"commands": commands})
    tg("setChatMenuButton", {"menu_button": {"type": "commands"}})


def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("sendMessage", payload)


def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("editMessageText", payload)


def delete_message(chat_id, message_id):
    return tg("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def answer_callback(callback_id, text=None, alert=False):
    payload = {"callback_query_id": callback_id, "show_alert": alert}
    if text:
        payload["text"] = text
    return tg("answerCallbackQuery", payload)


def fmt_amount(value):
    try:
        return f"{int(value):,}"
    except Exception:
        return "0"


def get_user(chat_id):
    if chat_id not in users:
        users[chat_id] = {
            "balance": 0,
            "ichancy_username": None,
            "ichancy_password": None,
            "password_visible": False,
            "bonuses": dict(DEFAULT_BONUSES),
            "history": [],
        }
    return users[chat_id]


def main_inline_keyboard():
    return inline([
        [cb("🎮 حساب iChancy 🎮", "account")],
        [cb("💳 شحن رصيد البوت", "topup"), cb("💸 سحب رصيد البوت", "withdraw_bot")],
        [cb("🎮 شحن حساب iChancy", "ichancy_deposit"), cb("💸 سحب من حساب iChancy", "ichancy_withdraw")],
        [cb("📋 سجل العمليات", "history"), cb("🎁 العروض والبونصات", "offers")],
        [cb("👥 نظام الإحالات", "referrals"), cb("🎟️ كود الهدية", "gift")],
        [cb("💬 الدعم والمساعدة", "support"), cb("📜 الشروط والخدمات", "terms")],
    ])


def remove_reply_keyboard():
    return {"remove_keyboard": True}


def inline(rows):
    return {"inline_keyboard": rows}


def cb(text, data):
    return {"text": text, "callback_data": data}


def nav_row(back_data="home", back_label="🔙 رجوع"):
    """Single inline back button. Telegram handles the native START button itself."""
    return [cb(back_label, back_data)]


def url_btn(text, url):
    return {"text": text, "url": url}


def copy_btn(text, value):
    # Telegram Bot API supports copy_text buttons.
    return {"text": text, "copy_text": {"text": str(value)}}


def greeting(chat_id):
    u = get_user(chat_id)
    return (
        "👑 <b>اهــــلا بالمــــــلك</b> 👑\n"
        f"🆔 معرفك على البوت: <code>{chat_id}</code>\n"
        f"💰 رصيدك: <b>{fmt_amount(u['balance'])}</b>\n"
        "👑 <b>نفتخر بانضمامك يامــلك</b> 👑"
    )


def set_panel(chat_id, text, reply_markup=None, force_new=False):
    """Edit the current bot panel when possible; otherwise send a new one."""
    mid = panel_message_ids.get(chat_id)
    if mid and not force_new:
        res = edit_message(chat_id, mid, text, reply_markup)
        if res.get("ok"):
            return res

    res = send_message(chat_id, text, reply_markup)
    if res.get("ok") and res.get("result"):
        panel_message_ids[chat_id] = res["result"]["message_id"]
    return res


def show_home(chat_id):
    flows.pop(chat_id, None)
    ensure_native_menu()

    # Remove any old persistent Reply Keyboard with a separate message.
    # Important: do NOT try to convert that same message into an Inline Keyboard;
    # Telegram may treat the text as unchanged and skip the edit.
    cleanup = send_message(chat_id, "⌨️ تم تحديث القائمة", remove_reply_keyboard())

    # Always send the actual home panel as a fresh message with Inline buttons.
    panel_message_ids.pop(chat_id, None)
    res = send_message(chat_id, greeting(chat_id), main_inline_keyboard())
    if res.get("ok") and res.get("result"):
        panel_message_ids[chat_id] = res["result"]["message_id"]

    # Keep the chat clean after Telegram has processed the keyboard removal.
    if cleanup.get("ok") and cleanup.get("result"):
        delete_message(chat_id, cleanup["result"]["message_id"])

    return res


def show_account(chat_id, created=False):
    u = get_user(chat_id)
    flows.pop(chat_id, None)
    if not u["ichancy_username"]:
        text = (
            "🎮 <b>حساب iChancy</b> 🎮\n\n"
            "لا يوجد حساب iChancy مرتبط بحسابك حاليًا."
        )
        markup = inline([
            [cb("➕ إنشاء حساب جديد", "ichancy_create")],
            nav_row("home"),
        ])
        return set_panel(chat_id, text, markup)

    pwd = u["ichancy_password"] or ""
    shown = html.escape(pwd) if u.get("password_visible") else "••••••••"
    username = html.escape(u["ichancy_username"])
    title = "✅ <b>تم إنشاء حسابك بنجاح</b>" if created else "🔐 <b>بيانات تسجيل الدخول للحساب</b>"
    text = (
        f"{title}\n\n"
        f"👤 اسم المستخدم: <code>{username}</code>\n"
        f"🔑 كلمة المرور: <code>{shown}</code>"
    )
    toggle_label = "🙈 إخفاء كلمة المرور" if u.get("password_visible") else "👁 عرض كلمة المرور"
    markup = inline([
        [copy_btn("📋 نسخ اسم المستخدم", u["ichancy_username"])],
        [cb(toggle_label, "ichancy_toggle_password")],
        [copy_btn("📋 نسخ كلمة المرور", pwd)],
        [url_btn("🌐 الدخول إلى iChancy", "https://www.ichancy200.com")],
        nav_row("home"),
    ])
    return set_panel(chat_id, text, markup)


def show_topup(chat_id):
    u = get_user(chat_id)
    b = u["bonuses"]
    text = (
        "💳 <b>شحن رصيد البوت</b>\n\n"
        f"💰 رصيدك الحالي: <b>{fmt_amount(u['balance'])}</b>\n\n"
        "🎁 <b>البونصات المتاحة حاليًا</b>\n"
        f"🟩 شام كاش: <b>+{b['sham']}%</b>\n"
        f"🔴 سيريتيل كاش: <b>+{b['syriatel']}%</b>\n"
        f"🟢 USDT: <b>+{b['usdt']}%</b>\n"
        f"🟣 ويش موني: <b>+{b['wish']}%</b>\n\n"
        "اختر وسيلة الشحن المناسبة:"
    )
    markup = inline([
        [cb("🟩 شام كاش", "topup_sham"), cb("🔴 سيريتيل كاش", "topup_syriatel")],
        [cb("🟢 USDT", "topup_usdt"), cb("🟣 ويش موني", "topup_wish")],
        nav_row("home"),
    ])
    return set_panel(chat_id, text, markup)


def show_usdt_networks(chat_id):
    text = (
        "🟢 <b>الشحن عبر USDT</b>\n\n"
        "اختر شبكة التحويل:"
    )
    markup = inline([
        [cb("🔴 USDT TRC20", "usdt_trc20")],
        [cb("🟡 USDT BEP20", "usdt_bep20")],
        nav_row("topup"),
    ])
    return set_panel(chat_id, text, markup)


def show_payment_placeholder(chat_id, title):
    text = (
        f"{title}\n\n"
        "🚧 <b>واجهة وسيلة الدفع جاهزة.</b>\n"
        "سيتم ربط التحقق التلقائي بالـAPI بعد اعتماد تصميم البوت بالكامل."
    )
    return set_panel(chat_id, text, inline([nav_row("topup")]))


def show_withdraw_bot(chat_id):
    u = get_user(chat_id)
    text = (
        "💸 <b>سحب رصيد البوت</b>\n\n"
        f"💰 رصيدك الحالي: <b>{fmt_amount(u['balance'])}</b>\n\n"
        "اختر طريقة السحب المناسبة:"
    )
    markup = inline([
        [cb("🟩 شام كاش", "wd_sham"), cb("🔴 سيريتيل كاش", "wd_syriatel")],
        [cb("🟢 USDT", "wd_usdt"), cb("🟣 ويش موني", "wd_wish")],
        nav_row("home"),
    ])
    return set_panel(chat_id, text, markup)


def show_withdraw_method(chat_id, method_name):
    u = get_user(chat_id)
    flows[chat_id] = {"step": "withdraw_bot_amount", "method": method_name}
    text = (
        f"💸 <b>السحب عبر {html.escape(method_name)}</b>\n\n"
        f"💰 رصيدك الحالي: <b>{fmt_amount(u['balance'])}</b>\n\n"
        "أدخل المبلغ المطلوب سحبه من رصيد البوت."
    )
    return set_panel(chat_id, text, inline([nav_row("withdraw_bot", "🔙 إلغاء")]))


def show_ichancy_deposit(chat_id):
    u = get_user(chat_id)
    if not u["ichancy_username"]:
        text = (
            "⚠️ <b>لا يوجد حساب iChancy مرتبط بحسابك.</b>\n\n"
            "أنشئ حساب iChancy أولًا للمتابعة."
        )
        return set_panel(chat_id, text, inline([
            [cb("🎮 إنشاء حساب iChancy", "ichancy_create")],
            nav_row("home"),
        ]))
    flows[chat_id] = {"step": "ichancy_deposit_amount"}
    text = (
        "🎮 <b>شحن حساب iChancy</b>\n\n"
        f"💰 رصيدك المتاح: <b>{fmt_amount(u['balance'])}</b>\n\n"
        "أدخل المبلغ الذي ترغب بإضافته إلى حسابك."
    )
    return set_panel(chat_id, text, inline([nav_row("home", "🔙 إلغاء")]))


def show_ichancy_withdraw(chat_id):
    u = get_user(chat_id)
    if not u["ichancy_username"]:
        text = (
            "⚠️ <b>لا يوجد حساب iChancy مرتبط بحسابك.</b>\n\n"
            "أنشئ حساب iChancy أولًا للمتابعة."
        )
        return set_panel(chat_id, text, inline([
            [cb("🎮 إنشاء حساب iChancy", "ichancy_create")],
            nav_row("home"),
        ]))
    # Bridge is not connected in UI prototype, so we intentionally show the approved message.
    text = (
        "⚠️ <b>الخدمة غير متاحة مؤقتًا</b>\n\n"
        "يرجى المحاولة بعد قليل."
    )
    return set_panel(chat_id, text, inline([nav_row("home")]))


def show_history(chat_id):
    u = get_user(chat_id)
    if not u["history"]:
        text = "📋 <b>سجل العمليات</b>\n\nلا توجد عمليات مسجلة حتى الآن."
    else:
        lines = ["📋 <b>آخر عملياتك</b>", ""]
        for item in u["history"][-10:][::-1]:
            lines.append(item)
            lines.append("")
        text = "\n".join(lines).rstrip()
    return set_panel(chat_id, text, inline([nav_row("home")]))


def show_offers(chat_id):
    b = get_user(chat_id)["bonuses"]
    text = (
        "🎁 <b>العروض والبونصات الحالية</b>\n\n"
        f"🟩 شام كاش: <b>+{b['sham']}%</b>\n"
        f"🔴 سيريتيل كاش: <b>+{b['syriatel']}%</b>\n"
        f"🟢 USDT: <b>+{b['usdt']}%</b>\n"
        f"🟣 ويش موني: <b>+{b['wish']}%</b>\n\n"
        "استفد من البونص عند شحن رصيد البوت عبر الوسيلة التي عليها عرض."
    )
    return set_panel(chat_id, text, inline([
        [cb("💳 شحن رصيد البوت", "topup")],
        nav_row("home"),
    ]))


def show_referrals(chat_id, bot_username=None):
    bot_username = bot_username or os.environ.get("BOT_USERNAME", "YourBot")
    link = f"https://t.me/{bot_username}?start=ref_{chat_id}"
    text = (
        "👥 <b>نظام الإحالات</b>\n\n"
        "شارك رابطك الخاص وادعُ أصدقاءك للانضمام إلى البوت.\n\n"
        "👤 عدد الأشخاص المسجلين عن طريقك: <b>0</b>\n"
        "💰 أرباح الإحالات: <b>0</b>\n\n"
        f"🔗 رابط الإحالة الخاص بك:\n<code>{html.escape(link)}</code>"
    )
    return set_panel(chat_id, text, inline([
        [copy_btn("📋 نسخ رابط الإحالة", link)],
        nav_row("home"),
    ]))


def show_gift(chat_id):
    flows[chat_id] = {"step": "gift_code"}
    text = (
        "🎟️ <b>كود الهدية</b>\n\n"
        "أدخل كود الهدية للاستفادة من المكافأة.\n\n"
        "كل كود صالح لاستخدام واحد فقط على مستوى البوت بالكامل."
    )
    return set_panel(chat_id, text, inline([nav_row("home", "🔙 إلغاء")]))


def support_usernames():
    raw = os.environ.get("SUPPORT_USERNAMES", "")
    result = []
    for x in raw.split(","):
        x = x.strip().lstrip("@")
        if x and re.fullmatch(r"[A-Za-z0-9_]{5,32}", x):
            result.append(x)
    return result


def show_support(chat_id):
    names = support_usernames()
    text = "👨‍💻 <b>تواصل مع خدمة العملاء</b>"
    rows = []
    if names:
        for name in names:
            rows.append([url_btn(f"👨‍💻 @{name}", f"https://t.me/{name}")])
    else:
        text += "\n\nلم تتم إضافة ممثل خدمة عملاء بعد."
    rows.append(nav_row("home"))
    return set_panel(chat_id, text, inline(rows))


def show_terms(chat_id):
    text = (
        "📜 <b>الشروط والخدمات</b>\n\n"
        "يُرجى قراءة الشروط بعناية قبل استخدام الخدمات.\n\n"
        "• تأكد من صحة بيانات حساب iChancy قبل تنفيذ أي عملية.\n"
        "• طلبات سحب رصيد البوت تخضع لمراجعة الإدارة قبل التنفيذ.\n"
        "• بعد تقديم طلب السحب، تتم معالجة الحوالة بأسرع وقت ممكن، وعادةً خلال ساعة إلى 3 ساعات.\n"
        "• يجب إدخال بيانات وسيلة الاستلام بشكل صحيح.\n"
        "• يحق للإدارة مراجعة أي عملية مشبوهة لحماية الحسابات والأرصدة.\n"
        "• نسب البونص والعروض قابلة للتغيير وتظهر داخل قسم شحن رصيد البوت.\n\n"
        "👑 شكرًا لثقتكم بخدماتنا"
    )
    return set_panel(chat_id, text, inline([nav_row("home")]))


def parse_amount(text):
    cleaned = text.replace(",", "").replace(" ", "").strip()
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    return value if value > 0 else None


def process_text_input(chat_id, text):
    flow = flows.get(chat_id)
    if not flow:
        return False

    step = flow.get("step")
    u = get_user(chat_id)

    if step == "ichancy_username":
        candidate = text.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{4,24}", candidate):
            set_panel(
                chat_id,
                "👤 <b>اكتب اسم المستخدم</b>\n\n"
                "اسم المستخدم غير صالح.",
                inline([nav_row("account", "🔙 إلغاء")])
            )
            return True
        flow["username"] = candidate
        flow["step"] = "ichancy_password"
        set_panel(
            chat_id,
            "🔐 <b>اكتب كلمة المرور</b>",
            inline([nav_row("account", "🔙 إلغاء")])
        )
        return True

    if step == "ichancy_password":
        password = text.strip()
        if len(password) < 6 or len(password) > 64:
            set_panel(
                chat_id,
                "🔐 <b>اكتب كلمة المرور</b>\n\n"
                "كلمة المرور قصيرة جدًا.",
                inline([nav_row("account", "🔙 إلغاء")])
            )
            return True
        # UI prototype only: save locally in memory. No real iChancy request is sent.
        u["ichancy_username"] = flow["username"]
        u["ichancy_password"] = password
        u["password_visible"] = False
        flows.pop(chat_id, None)
        # Keep everything in ONE panel: success title + credentials + buttons.
        show_account(chat_id, created=True)
        return True

    if step == "withdraw_bot_amount":
        amount = parse_amount(text)
        if amount is None:
            set_panel(chat_id, "⚠️ أدخل مبلغًا صحيحًا بالأرقام فقط.", inline([nav_row("withdraw_bot", "🔙 إلغاء")]))
            return True
        if amount > u["balance"]:
            set_panel(
                chat_id,
                "⚠️ <b>الرصيد غير كافٍ</b>\n\n"
                f"رصيدك الحالي: <b>{fmt_amount(u['balance'])}</b>\n"
                f"المبلغ المطلوب: <b>{fmt_amount(amount)}</b>",
                inline([[cb("🔙 رجوع", "withdraw_bot")]])
            )
            flows.pop(chat_id, None)
            return True
        method = flow.get("method", "غير محدد")
        flows.pop(chat_id, None)
        # No real balance mutation in the UI-only prototype.
        text2 = (
            "✅ <b>تم استلام طلب السحب</b>\n\n"
            f"💰 المبلغ: <b>{fmt_amount(amount)}</b>\n"
            f"💳 طريقة الاستلام: <b>{html.escape(method)}</b>\n\n"
            "⏱️ سيتم تنفيذ طلبك بأسرع وقت ممكن، وعادةً خلال مدة تتراوح بين ساعة و3 ساعات.\n"
            "سيتم إشعارك فور اكتمال الحوالة. 👑\n\n"
            "🧪 <i>طلب تجريبي فقط، لم يتم خصم أو تحويل أي رصيد.</i>"
        )
        set_panel(chat_id, text2, inline([nav_row("home")]))
        return True

    if step == "ichancy_deposit_amount":
        amount = parse_amount(text)
        if amount is None:
            set_panel(chat_id, "⚠️ أدخل مبلغًا صحيحًا بالأرقام فقط.", inline([nav_row("home", "🔙 إلغاء")]))
            return True
        flows.pop(chat_id, None)
        if amount > u["balance"]:
            set_panel(
                chat_id,
                "⚠️ <b>الرصيد غير كافٍ</b>\n\n"
                f"رصيدك الحالي: <b>{fmt_amount(u['balance'])}</b>\n"
                f"المبلغ المطلوب: <b>{fmt_amount(amount)}</b>",
                inline([nav_row("home")])
            )
            return True
        set_panel(
            chat_id,
            "🧪 <b>الواجهة جاهزة للاختبار</b>\n\n"
            "لم يتم إرسال أي مبلغ إلى iChancy لأن الجسر لم يتم ربطه بعد.",
            inline([nav_row("home")])
        )
        return True

    if step == "gift_code":
        flows.pop(chat_id, None)
        set_panel(
            chat_id,
            "❌ <b>كود الهدية غير صالح أو غير موجود.</b>\n\n"
            "سيتم تفعيل إدارة أكواد الهدايا عند بناء لوحة الإدارة.",
            inline([nav_row("home")])
        )
        return True

    return False


def handle_menu_text(chat_id, text):
    mapping = {
        "🎮 حساب iChancy 🎮": show_account,
        "💳 شحن رصيد البوت": show_topup,
        "💸 سحب رصيد البوت": show_withdraw_bot,
        "🎮 شحن حساب iChancy": show_ichancy_deposit,
        "💸 سحب من حساب iChancy": show_ichancy_withdraw,
        "📋 سجل العمليات": show_history,
        "🎁 العروض والبونصات": show_offers,
        "👥 نظام الإحالات": show_referrals,
        "🎟️ كود الهدية": show_gift,
        "💬 الدعم والمساعدة": show_support,
        "📜 الشروط والخدمات": show_terms,
    }
    fn = mapping.get(text)
    if fn:
        fn(chat_id)
        return True
    return False


def handle_callback(query):
    callback_id = query.get("id")
    data = query.get("data", "")
    message = query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        return
    if message.get("message_id"):
        panel_message_ids[chat_id] = message["message_id"]

    answer_callback(callback_id)
    u = get_user(chat_id)

    if data == "home":
        flows.pop(chat_id, None)
        set_panel(chat_id, greeting(chat_id), main_inline_keyboard())
    elif data == "account":
        show_account(chat_id)
    elif data == "ichancy_create":
        flows[chat_id] = {"step": "ichancy_username"}
        set_panel(
            chat_id,
            "👤 <b>اكتب اسم المستخدم</b>",
            inline([nav_row("account", "🔙 إلغاء")])
        )
    elif data == "ichancy_toggle_password":
        u["password_visible"] = not u.get("password_visible")
        show_account(chat_id)
    elif data == "topup":
        show_topup(chat_id)
    elif data == "topup_usdt":
        show_usdt_networks(chat_id)
    elif data == "topup_sham":
        show_payment_placeholder(chat_id, "🟩 <b>الشحن عبر شام كاش</b>")
    elif data == "topup_syriatel":
        show_payment_placeholder(chat_id, "🔴 <b>الشحن عبر سيريتيل كاش</b>")
    elif data == "topup_wish":
        show_payment_placeholder(chat_id, "🟣 <b>الشحن عبر ويش موني</b>")
    elif data == "usdt_trc20":
        show_payment_placeholder(chat_id, "🔴 <b>USDT - TRC20</b>")
    elif data == "usdt_bep20":
        show_payment_placeholder(chat_id, "🟡 <b>USDT - BEP20</b>")
    elif data == "withdraw_bot":
        flows.pop(chat_id, None)
        show_withdraw_bot(chat_id)
    elif data == "wd_sham":
        show_withdraw_method(chat_id, "شام كاش")
    elif data == "wd_syriatel":
        show_withdraw_method(chat_id, "سيريتيل كاش")
    elif data == "wd_usdt":
        show_withdraw_method(chat_id, "USDT")
    elif data == "wd_wish":
        show_withdraw_method(chat_id, "ويش موني")
    elif data == "ichancy_deposit":
        show_ichancy_deposit(chat_id)
    elif data == "ichancy_withdraw":
        show_ichancy_withdraw(chat_id)
    elif data == "history":
        show_history(chat_id)
    elif data == "offers":
        show_offers(chat_id)
    elif data == "referrals":
        show_referrals(chat_id)
    elif data == "gift":
        show_gift(chat_id)
    elif data == "support":
        show_support(chat_id)
    elif data == "terms":
        show_terms(chat_id)
    else:
        answer_callback(callback_id, "هذا الخيار قيد التجهيز", True)


@app.route("/")
def home():
    return "Asmar Robert Bot UI prototype is running ✅"


@app.route("/health")
def health():
    return jsonify({"ok": True, "mode": "ui-prototype"})


@app.route("/webhook", methods=["POST"])
def webhook():
    received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if received_secret != WEBHOOK_SECRET:
        return jsonify({"ok": False}), 403

    update = request.get_json(silent=True) or {}

    if update.get("callback_query"):
        handle_callback(update["callback_query"])
        return jsonify({"ok": True})

    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    text = message.get("text", "")
    message_id = message.get("message_id")

    if not chat_id:
        return jsonify({"ok": True})

    get_user(chat_id)

    if text.startswith("/start") or text.startswith("/menu"):
        flows.pop(chat_id, None)
        show_home(chat_id)
        return jsonify({"ok": True})

    if text == "/myid":
        send_message(chat_id, f"🆔 معرفك على البوت: <code>{chat_id}</code>")
        return jsonify({"ok": True})

    # Admin-only temporary UI testing helper. It DOES NOT move real money.
    # Example: /testcredit 200000
    if text.startswith("/testcredit") and chat_id == ADMIN_ID:
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            amount = parse_amount(parts[1])
            if amount is not None:
                get_user(chat_id)["balance"] = amount
                send_message(chat_id, f"🧪 تم ضبط الرصيد التجريبي إلى <b>{fmt_amount(amount)}</b>")
                return jsonify({"ok": True})
        send_message(chat_id, "الاستخدام: <code>/testcredit 200000</code>")
        return jsonify({"ok": True})

    # Keep the conversation visually clean: remove menu/input messages when possible.
    if message_id:
        delete_message(chat_id, message_id)

    if process_text_input(chat_id, text):
        return jsonify({"ok": True})

    if handle_menu_text(chat_id, text):
        return jsonify({"ok": True})

    # Unknown text: keep user inside the designed interface.
    set_panel(
        chat_id,
        "👑 اختر الخدمة المطلوبة من القائمة.",
        main_inline_keyboard()
    )
    return jsonify({"ok": True})


@app.route("/set-webhook")
def set_webhook():
    ensure_native_menu()
    webhook_url = f"{PUBLIC_BASE_URL}/webhook"
    response = tg("setWebhook", {
        "url": webhook_url,
        "secret_token": WEBHOOK_SECRET,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    })
    return jsonify(response)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
