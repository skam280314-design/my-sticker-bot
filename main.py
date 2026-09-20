import threading
import os
from flask import Flask

from bot import main as start_bot

app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is running!", 200

def run_bot():
    start_bot()

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)