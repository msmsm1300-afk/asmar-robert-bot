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
        json={"chat_id": chat_id, "text": text},
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

    # اختبار Agent API الجديد ببيانات وهمية
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

            server = r.headers.get("server", "")
            content_type = r.headers.get("content-type", "")
            body = r.text.lower()

            if "invalid username or password" in body:
                send_message(
                    chat_id,
                    f"✅ Agent API قابل للوصول\nHTTP {r.status_code}"
                )

            elif r.status_code == 403 and server.lower() == "cloudflare":
                send_message(
                    chat_id,
                    "⛔ Agent API محجوب بواسطة Cloudflare\n"
                    "HTTP 403"
                )

            else:
                send_message(
                    chat_id,
                    "⚠️ رد Agent API\n"
                    f"HTTP {r.status_code}\n"
                    f"Server: {server}\n"
                    f"Content-Type: {content_type}"
                )

        except Exception:
            send_message(
                chat_id,
                "❌ تعذر الاتصال بـ Agent API"
            )

    # اختبار API لوحة الكاشيرة الداخلية ببيانات وهمية
    elif text == "/paneldummy":

        if chat_id != ADMIN_ID:
            send_message(chat_id, "⛔ غير مصرح")
            return jsonify({"ok": True})

        try:
            r = requests.post(
                "https://agents.ichancy.com/global/api/User/signIn",
                json={
                    "username": "definitely-not-real@example.invalid",
                    "password": "dummy-password-123456"
                },
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Origin": "https://agents.ichancy.com",
                    "Referer": "https://agents.ichancy.com/"
                },
                timeout=20
            )

            server = r.headers.get("server", "")
            content_type = r.headers.get("content-type", "")

            if r.status_code == 403 and server.lower() == "cloudflare":

                send_message(
                    chat_id,
                    "⛔ API لوحة الكاشيرة أيضاً محجوب من Render\n\n"
                    "HTTP 403\n"
                    "Server: cloudflare"
                )

            elif "application/json" in content_type.lower():

                send_message(
                    chat_id,
                    "✅ Render وصل إلى API لوحة الكاشيرة الداخلية\n\n"
                    f"HTTP {r.status_code}\n"
                    f"Server: {server}"
                )

            else:

                send_message(
                    chat_id,
                    "⚠️ وصل رد مختلف من API لوحة الكاشيرة\n\n"
                    f"HTTP {r.status_code}\n"
                    f"Server: {server}\n"
                    f"Content-Type: {content_type}"
                )

        except Exception:

            send_message(
                chat_id,
                "❌ تعذر الوصول إلى API لوحة الكاشيرة"
            )

    else:

        send_message(chat_id, "وصلتني رسالتك ✅")

    return jsonify({"ok": True})


@app.route("/set-webhook")
def set_webhook():

    webhook_url = "https://asmar-robert-bot.onrender.com/webhook"

    response = requests.post(
        f"{TG_API}/setWebhook",
        json={
            "url": webhook_url,
            "secret_token": WEBHOOK_SECRET
        },
        timeout=15
    )

    return response.text
