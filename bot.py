import telebot
import requests
import psycopg
import json
import re
from flask import Flask, jsonify, request
from flask_cors import CORS
import threading
import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8803648566:AAG1BL4brhrA8PkJ2TD30qm3CPE4ZNqdnts")
ADMIN_CHAT_ID = 7929131842
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://mapbotuser:r47l5ou0pDueVnus4tiD3d2w7hYQJ1vy@dpg-db4fc5ks728c73ajgig0-a.frankfurt-postgres.render.com/mapbotdb")

app = Flask(__name__)
CORS(app)

# 🛡️ ГЛОБАЛЬНЫЙ ПЕРЕХВАТЧИК OPTIONS (Решает проблему CORS preflight)
@app.before_request
def handle_options():
    if request.method == 'OPTIONS':
        return '', 200

# 🛡️ ДОБАВЛЕНИЕ CORS ЗАГОЛОВКОВ К КАЖДОМУ ОТВЕТУ
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
        conn = get_db_connection()
        c = conn.cursor()
        
        c.execute('''CREATE TABLE IF NOT EXISTS points
                     (id SERIAL PRIMARY KEY, title TEXT, address TEXT, lat REAL, lon REAL, status TEXT DEFAULT 'active')''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS parkings
                     (id SERIAL PRIMARY KEY, title TEXT UNIQUE, address TEXT, lat REAL, lon REAL, 
                      available_count TEXT DEFAULT '?', dead_count TEXT DEFAULT '?', broken_count TEXT DEFAULT '?')''')
        
        c.execute("DROP TABLE IF EXISTS zones")
        c.execute('''CREATE TABLE zones
                     (id SERIAL PRIMARY KEY, coords TEXT, comment TEXT)''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS departures
                     (id SERIAL PRIMARY KEY, point_id INTEGER UNIQUE, point_title TEXT, person_name TEXT)''')
        
        conn.commit()
        conn.close()
        print("✅ База данных успешно инициализирована!")
    except Exception as e:
        print(f"❌ Ошибка инициализации БД: {e}")

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

    # ==========================================
    # 1. СОЗДАНИЕ ПАРКОВКИ (старый проверенный формат)
    # ==========================================
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
                conn = get_db_connection()
                c = conn.cursor()
                c.execute("INSERT INTO parkings (title, address, lat, lon) VALUES (%s, %s, %s, %s)", (title, location_part, lat, lon))
                conn.commit()
                conn.close()
                bot.reply_to(message, f"✅ Парковка '{title}' добавлена!")
            else:
                bot.reply_to(message, f"❌ Не найдено: '{location_part}'")
        return

    # ==========================================
    # 2. УПРАВЛЕНИЕ ПАРКОВКАМИ ЧЕРЕЗ ДЕФИС (Новое)
    # ==========================================
    if ' - ' in text:
        parts = text.split(' - ', 1) # Разделяем только по первому ' - '
        title = parts[0].strip()
        action = parts[1].strip()
        
        # А) Удаление парковки
        if action.lower() == 'удалить':
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("DELETE FROM parkings WHERE title = %s", (title,))
            if c.rowcount > 0:
                bot.reply_to(message, f"🗑️ Парковка '{title}' успешно удалена.")
            else:
                bot.reply_to(message, f"❌ Парковка '{title}' не найдена.")
            conn.commit()
            conn.close()
            return
        
        # Б) Обновление статистики (С, Д, П)
        # Ищем паттерны типа С5, д?, П12 (регистронезависимо)
        params_match = re.findall(r'([СсДдПп])(\d+|\?)', action)
        
        if params_match:
            avail = dead = broken = None
            for param_type, param_value in params_match:
                p_type = param_type.lower()
                if p_type == 'с': dead = param_value
                elif p_type == 'д': avail = param_value
                elif p_type == 'п': broken = param_value
            
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT available_count, dead_count, broken_count FROM parkings WHERE title = %s", (title,))
            row = c.fetchone()
            
            if row:
                # Если параметр не передан, оставляем старое значение из БД
                new_avail = avail if avail is not None else row[0]
                new_dead = dead if dead is not None else row[1]
                new_broken = broken if broken is not None else row[2]
                
                c.execute("UPDATE parkings SET available_count = %s, dead_count = %s, broken_count = %s WHERE title = %s", 
                         (new_avail, new_dead, new_broken, title))
                conn.commit()
                conn.close()
                
                response_parts = []
                if avail is not None: response_parts.append(f"Д:{new_avail}")
                if dead is not None: response_parts.append(f"С:{new_dead}")
                if broken is not None: response_parts.append(f"П:{new_broken}")
                
                bot.reply_to(message, f"✅ '{title}' обновлена: {', '.join(response_parts)}")
            else:
                bot.reply_to(message, f"❌ Парковка '{title}' не найдена.")
            return

    # ==========================================
    # 3. СОЗДАНИЕ ОБЫЧНОЙ ТОЧКИ (САМОКАТА)
    # ==========================================
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
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("INSERT INTO points (title, address, lat, lon, status) VALUES (%s, %s, %s, %s, 'active')", (title, location_part, lat, lon))
            conn.commit()
            conn.close()
            bot.reply_to(message, f"✅ Точка '{title}' добавлена!")
        else:
            bot.reply_to(message, f"❌ Не найдено: '{location_part}'")

    # ==========================================
    # 4. КОМАНДЫ УДАЛЕНИЯ ВСЕХ / ПОСЛЕДНЕЙ ПАРКОВКИ
    # ==========================================
    if text_lower == 'все p off':
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM parkings")
        count = c.rowcount
        conn.commit()
        conn.close()
        bot.reply_to(message, f"🗑️ Удалено всех парковок: {count}")
        return

    if text_lower == 'last p off':
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM parkings WHERE id = (SELECT MAX(id) FROM parkings)")
        if c.rowcount > 0:
            bot.reply_to(message, "🗑️ Последняя парковка успешно удалена.")
        else:
            bot.reply_to(message, "❌ Парковок не найдено.")
        conn.commit()
        conn.close()
        return

# --- API ЭНДПОИНТЫ (Без изменений, всё работает) ---

@app.route('/get_points')
def get_points():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, title, address, lat, lon, status FROM points WHERE status != 'found'")
    points = [{"id": r[0], "title": r[1], "address": r[2], "lat": r[3], "lon": r[4], "status": r[5]} for r in c.fetchall()]
    conn.close()
    return jsonify(points)

@app.route('/update_status', methods=['POST'])
def update_status():
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

@app.route('/get_parkings')
def get_parkings():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, title, address, lat, lon, available_count, dead_count, broken_count FROM parkings")
    parkings = [{"id": r[0], "title": r[1] or "?", "address": r[2] or "?", "lat": float(r[3]), "lon": float(r[4]), 
                 "available": r[5] or '?', "dead": r[6] or '?', "broken": r[7] or '?'} for r in c.fetchall() if r[3] and r[4]]
    conn.close()
    return jsonify(parkings)

@app.route('/update_parking', methods=['POST'])
def update_parking():
    data = request.json
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE parkings SET available_count = %s, dead_count = %s, broken_count = %s WHERE id = %s", 
              (data.get('available', '?'), data.get('dead', '?'), data.get('broken', '?'), data.get('id')))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/get_zones', methods=['GET'])
def get_zones():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, coords, comment FROM zones")
    zones = []
    for r in c.fetchall():
        try:
            coords_list = json.loads(r[1]) if r[1] else []
            zones.append({"id": r[0], "coords": coords_list, "comment": r[2] or ""})
        except:
            continue
    conn.close()
    return jsonify(zones)

@app.route('/add_zone', methods=['POST'])
def add_zone():
    data = request.json
    coords = data.get('coords')
    comment = data.get('comment', '')
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO zones (coords, comment) VALUES (%s, %s)", (json.dumps(coords), comment))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/delete_zone', methods=['POST'])
def delete_zone():
    data = request.json
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM zones WHERE id = %s", (data.get('id'),))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route('/get_departures')
def get_departures():
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT point_id, point_title, person_name FROM departures")
        departures = [{"point_id": r[0], "point_title": r[1], "person_name": r[2]} for r in c.fetchall()]
        conn.close()
        return jsonify(departures)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/add_departure', methods=['POST'])
def add_departure():
    try:
        data = request.json
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            INSERT INTO departures (point_id, point_title, person_name) 
            VALUES (%s, %s, %s)
            ON CONFLICT (point_id) DO UPDATE SET 
            point_title = EXCLUDED.point_title, 
            person_name = EXCLUDED.person_name
        """, (data.get('point_id'), data.get('point_title'), data.get('person_name')))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/remove_departure', methods=['POST'])
def remove_departure():
    try:
        data = request.json
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM departures WHERE point_id = %s", (data.get('point_id'),))
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
    app.run(host='0.0.0.0', port=8080)
