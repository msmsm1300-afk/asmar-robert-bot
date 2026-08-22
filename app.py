from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "Asmar Robert Bot is running ✅"
