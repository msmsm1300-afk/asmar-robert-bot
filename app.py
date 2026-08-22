import os
import hashlib
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0"))

ICHANCY_USERNAME = os.environ.get("ICHANCY_USERNAME")
ICHANCY_PASSWORD = os.environ.get("ICHANCY_PASSWORD")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

WEBHOOK_SECRET = hashlib.sha256(
    BOT_TOKEN.encode()
).hexdigest() if BOT_TOKEN else ""


def send_message(chat_id, text):
    requests.post(
        f"{TG_API}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text
        },
        timeout=15
    )


@app.route("/")
def home():
    return "Asmar Robert Bot is running ✅"


@app.route("/webhook", methods=["POST"])
def webhook():

    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )

    if received_secret != WEBHOOK_SECRET:
        return jsonify({"ok": False}), 403

    update = request.get_json(silent=True) or {}

    message = update.get("message", {})
    chat_id = (message.get("chat") or {}).get("id")
    text = message.get("text", "")

    if not chat_id:
        return jsonify({"ok": True})

    if text == "/start":

        send_message(
            chat_id,
            "أهلاً بك في Asmar Robert 🤖\n\n"
            "البوت شغّال بنجاح ✅"
        )

    elif text == "/myid":

        send_message(
            chat_id,
            f"Your Telegram ID: {chat_id}"
        )

    # اختبار وهمي لمعرفة هل Render يصل إلى iChancy
    elif text == "/ichancydummy":

        if chat_id != ADMIN_ID:
            send_message(chat_id, "⛔ غير مصرح")
            return jsonify({"ok": True})

        try:
            r = requests.post(
                "https://agents.ichancy.com/global/api/UserApi/signin",
                json={
                    "username": "definitely-not-real@example.invalid",
                    "password": "dummy-password-123456"
                },
                timeout=20
            )

            body = r.text.lower()
            headers_text = str(r.headers).lower()

            server = r.headers.get("server", "")
            content_type = r.headers.get("content-type", "")

            if "invalid username or password" in body:

                send_message(
                    chat_id,
                    "✅ Render وصل إلى Agent API\n\n"
                    f"HTTP {r.status_code}\n"
                    "رد API: Invalid username or password"
                )

            elif (
                r.status_code == 403
                and (
                    "cloudflare" in body
                    or "cf-ray" in headers_text
                    or "text/html" in content_type.lower()
                )
            ):

                send_message(
                    chat_id,
                    "⛔ الحظر من Cloudflare / الشبكة\n\n"
                    "HTTP 403\n"
                    f"Server: {server}"
                )

            else:

                send_message(
                    chat_id,
                    "⚠️ رد غير محسوم\n\n"
                    f"HTTP {r.status_code}\n"
                    f"Content-Type: {content_type}\n"
                    f"Server: {server}"
                )

        except Exception:

            send_message(
                chat_id,
                "❌ تعذر الوصول إلى iChancy من Render"
            )

    # اختبار تسجيل الدخول الحقيقي
    elif text == "/ichancytest":

        if chat_id != ADMIN_ID:
            send_message(chat_id, "⛔ غير مصرح")
            return jsonify({"ok": True})

        if not ICHANCY_USERNAME or not ICHANCY_PASSWORD:

            send_message(
                chat_id,
                "❌ بيانات iChancy غير موجودة على السيرفر"
            )

            return jsonify({"ok": True})

        try:
            r = requests.post(
                "https://agents.ichancy.com/global/api/UserApi/signin",
                json={
                    "username": ICHANCY_USERNAME,
                    "password": ICHANCY_PASSWORD
                },
                timeout=20
            )

            if r.status_code == 403:

                send_message(
                    chat_id,
                    "❌ iChancy رفض اتصال السيرفر\n"
                    "HTTP 403"
                )

            else:

                try:
                    data = r.json()
                except Exception:
                    data = {}

                result = data.get("result")

                if (
                    isinstance(result, dict)
                    and result.get("accessToken")
                    and result.get("refreshToken")
                ):

                    send_message(
                        chat_id,
                        "✅ تم تسجيل الدخول إلى iChancy "
                        "من السيرفر بنجاح"
                    )

                else:

                    send_message(
                        chat_id,
                        "❌ تسجيل الدخول لم ينجح\n"
                        f"HTTP {r.status_code}"
                    )

        except Exception:

            send_message(
                chat_id,
                "❌ تعذر الاتصال بـ iChancy من السيرفر"
            )

    else:

        send_message(
            chat_id,
            "وصلتني رسالتك ✅"
        )

    return jsonify({"ok": True})


@app.route("/set-webhook")
def set_webhook():

    webhook_url = (
        "https://asmar-robert-bot.onrender.com/webhook"
    )

    response = requests.post(
        f"{TG_API}/setWebhook",
        json={
            "url": webhook_url,
            "secret_token": WEBHOOK_SECRET
        },
        timeout=15
    )

    return response.text
