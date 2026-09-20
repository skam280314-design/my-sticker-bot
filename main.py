import threading
import os
from flask import Flask

from bot import main as start_bot

app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is running!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, use_reloader=False)

if __name__ == "__main__":
    # Flask — в фоне (в отдельном потоке)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Бот — в основном потоке (иначе set_wakeup_fd не работает)
    start_bot()