import telebot
import requests
import psycopg
import json
import re
from flask import Flask, jsonify, request
import threading
import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8803648566:AAHmG4XTMTDqfIHlWjBeDsKCGmQ18pxKnGQ")
ADMIN_CHAT_ID = 7929131842
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://mapbotuser:r47l5ou0pDueVnus4tiD3d2w7hYQJ1vy@dpg-db4fc5ks728c73ajgig0-a.frankfurt-postgres.render.com/mapbotdb")

app = Flask(__name__)

# 🛡️ ЖЕЛЕЗОБЕТОННЫЙ CORS: Применяется к КАЖДОМУ ответу сервера, включая тестовые OPTIONS запросы
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
    return response

def get_db_connection():
    return psycopg.connect(DATABASE_URL)

def init_db():
    try:
        print("🔄 Подключение к базе данных...")
        conn = get_db_connection()
        c = conn.cursor()
        
        c.execute('''CREATE TABLE IF NOT EXISTS points
                     (id SERIAL PRIMARY KEY, title TEXT, address TEXT, lat REAL, lon REAL, status TEXT DEFAULT 'active')''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS parkings
                     (id SERIAL PRIMARY KEY, title TEXT UNIQUE, address TEXT, lat REAL, lon REAL, 
                      available_count TEXT DEFAULT '?', dead_count TEXT DEFAULT '?', broken_count TEXT DEFAULT '?')''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS zones
                     (id SERIAL PRIMARY KEY, coords JSONB, comment TEXT)''')
        
        conn.commit()
        conn.close()
        print("✅ База данных успешно инициализирована!")
    except Exception as e:
        print(f"❌ КРИТИЧЕСКАЯ ОШИБКА БД: {e}")

init_db()

def get_coordinates(address):
    query = f"г. Смоленск, {address}"
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": query, "format": "json", "limit": 1}
    headers = {"User-Agent": "SmolenskMapBot/1.0"}
    try:
        response = requests.get(url, params=params, headers=headers, timeout=5)
        data = response.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except:
        pass
    return None, None

bot = telebot.TeleBot(BOT_TOKEN)

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if message.chat.id != ADMIN_CHAT_ID or message.text is None:
        return 

    text = message.text.strip()
    text_lower = text.lower()

    if text_lower == 'все p off':
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("DELETE FROM parkings")
            conn.commit()
            conn.close()
            bot.reply_to(message, "🗑️ Все парковки удалены.")
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {e}")
        return

    if text_lower == 'last p off':
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("DELETE FROM parkings WHERE id = (SELECT MAX(id) FROM parkings)")
            conn.commit()
            conn.close()
            bot.reply_to(message, "🗑️ Последняя парковка удалена.")
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {e}")
        return

    parking_match = re.match(r'^(.+?)\s+([ДдСсПп]\d+(?:\s+[ДдСсПп]\d+)*)$', text)
    if parking_match:
        title = parking_match.group(1).strip()
        params_str = parking_match.group(2)
        avail = dead = broken = None
        for param_type, param_value in re.findall(r'([ДдСсПп])(\d+)', params_str):
            if param_type.lower() == 'д': avail = param_value
            elif param_type.lower() == 'с': dead = param_value
            elif param_type.lower() == 'п': broken = param_value
        
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT available_count, dead_count, broken_count FROM parkings WHERE title = %s", (title,))
            row = c.fetchone()
            if row:
                new_avail = avail if avail else row[0]
                new_dead = dead if dead else row[1]
                new_broken = broken if broken else row[2]
                c.execute("UPDATE parkings SET available_count = %s, dead_count = %s, broken_count = %s WHERE title = %s", 
                         (new_avail, new_dead, new_broken, title))
                conn.commit()
                conn.close()
                bot.reply_to(message, f"✅ '{title}' обновлена: Д:{new_avail or '?'}, С:{new_dead or '?'}, П:{new_broken or '?'}")
            else:
                bot.reply_to(message, f"❌ Парковка '{title}' не найдена.")
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка БД: {e}")
        return

    if text_lower.endswith(' - p'):
        clean_text = text[:-4].strip()
        if '-' in clean_text:
            parts = clean_text.rsplit('-', 1)
            location_part, title = parts[0].strip(), parts[1].strip()
            coord_match = re.match(r'^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$', location_part)
            
            if coord_match:
                lat, lon = float(coord_match.group(1)), float(coord_match.group(2))
            else:
                bot.reply_to(message, f"⏳ Ищу '{location_part}'...")
                lat, lon = get_coordinates(location_part)
                
            if lat and lon:
                try:
                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute("INSERT INTO parkings (title, address, lat, lon) VALUES (%s, %s, %s, %s)", (title, location_part, lat, lon))
                    conn.commit()
                    conn.close()
                    bot.reply_to(message, f"✅ Парковка '{title}' добавлена!")
                except Exception as e:
                    bot.reply_to(message, f"❌ Ошибка: {e}")
            else:
                bot.reply_to(message, f"❌ Не найдено: '{location_part}'")
        return

    if '-' in text:
        parts = text.rsplit('-', 1)
        location_part, title = parts[0].strip(), parts[1].strip()
        coord_match = re.match(r'^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$', location_part)
        
        if coord_match:
            lat, lon = float(coord_match.group(1)), float(coord_match.group(2))
        else:
            bot.reply_to(message, f"⏳ Ищу '{location_part}'...")
            lat, lon = get_coordinates(location_part)
            
        if lat and lon:
            try:
                conn = get_db_connection()
                c = conn.cursor()
                c.execute("INSERT INTO points (title, address, lat, lon, status) VALUES (%s, %s, %s, %s, 'active')", (title, location_part, lat, lon))
                conn.commit()
                conn.close()
                bot.reply_to(message, f"✅ Точка '{title}' добавлена!")
            except Exception as e:
                bot.reply_to(message, f"❌ Ошибка: {e}")
        else:
            bot.reply_to(message, f"❌ Не найдено: '{location_part}'")

# --- API ЭНДПОИНТЫ ---

@app.route('/get_points')
def get_points():
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, title, address, lat, lon, status FROM points WHERE status != 'found'")
        points = [{"id": r[0], "title": r[1], "address": r[2], "lat": r[3], "lon": r[4], "status": r[5]} for r in c.fetchall()]
        conn.close()
        return jsonify(points)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/update_status', methods=['POST'])
def update_status():
    try:
        data = request.json
        conn = get_db_connection()
        c = conn.cursor()
        if data.get('status') == 'found':
            c.execute("DELETE FROM points WHERE id = %s", (data.get('id'),))
        else:
            c.execute("UPDATE points SET status = %s WHERE id = %s", (data.get('status'), data.get('id')))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/get_parkings')
def get_parkings():
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, title, address, lat, lon, available_count, dead_count, broken_count FROM parkings")
        parkings = [{"id": r[0], "title": r[1] or "?", "address": r[2] or "?", "lat": float(r[3]), "lon": float(r[4]), 
                     "available": r[5] or '?', "dead": r[6] or '?', "broken": r[7] or '?'} for r in c.fetchall() if r[3] and r[4]]
        conn.close()
        return jsonify(parkings)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/update_parking', methods=['POST'])
def update_parking():
    try:
        data = request.json
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE parkings SET available_count = %s, dead_count = %s, broken_count = %s WHERE id = %s", 
                  (data.get('available', '?'), data.get('dead', '?'), data.get('broken', '?'), data.get('id')))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/get_zones', methods=['GET', 'OPTIONS'])
def get_zones():
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, coords, comment FROM zones")
        zones = [{"id": r[0], "coords": r[1], "comment": r[2]} for r in c.fetchall()]
        conn.close()
        return jsonify(zones)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/add_zone', methods=['POST', 'OPTIONS'])
def add_zone():
    try:
        data = request.json
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("INSERT INTO zones (coords, comment) VALUES (%s, %s)", (json.dumps(data.get('coords')), data.get('comment', '')))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/delete_zone', methods=['POST', 'OPTIONS'])
def delete_zone():
    try:
        data = request.json
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM zones WHERE id = %s", (data.get('id'),))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

def run_bot():
    bot.polling(none_stop=True)

if __name__ == '__main__':
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    # Жестко задаем порт 8080, который требует Render
    app.run(host='0.0.0.0', port=8080)
