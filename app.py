import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def send_message(chat_id, text):
    requests.post(
        f"{TG_API}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text
        },
        timeout=10
    )

@app.route("/")
def home():
    return "Asmar Robert Bot is running ✅"

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}

    message = update.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text", "")

    if chat_id:
        if text == "/start":
            send_message(
                chat_id,
                "أهلاً بك في Asmar Robert 🤖\n\nالبوت شغّال بنجاح ✅"
            )
        else:
            send_message(chat_id, "وصلتني رسالتك ✅")

    return jsonify({"ok": True})

@app.route("/set-webhook")
def set_webhook():
    webhook_url = "https://asmar-robert-bot.onrender.com/webhook"

    response = requests.get(
        f"{TG_API}/setWebhook",
        params={"url": webhook_url},
        timeout=10
    )

    return response.text
