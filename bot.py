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
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://mapbotuser:r47l5ou0pDueVnus4tiD3d2w7hYQJ1vy@dpg-db4fc5ks728c73ajgig0-a.frankfurt-postgres.render.com/mapbotdb")

app = Flask(__name__)
CORS(app)

@app.before_request
def handle_options():
    if request.method == 'OPTIONS':
        return '', 200

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
        
        try:
            c.execute("ALTER TABLE points ADD COLUMN comment TEXT")
        except psycopg.errors.DuplicateColumn:
            pass
        
        try:
            c.execute("ALTER TABLE points ADD COLUMN url TEXT")
        except psycopg.errors.DuplicateColumn:
            pass
        
        c.execute('''CREATE TABLE IF NOT EXISTS parkings
                     (id SERIAL PRIMARY KEY, title TEXT UNIQUE, address TEXT, lat REAL, lon REAL, 
                      available_count TEXT DEFAULT '?', dead_count TEXT DEFAULT '?', broken_count TEXT DEFAULT '?')''')
        
        try:
            c.execute("ALTER TABLE parkings ADD COLUMN photo_url TEXT")
        except psycopg.errors.DuplicateColumn:
            pass
        
        c.execute('''CREATE TABLE IF NOT EXISTS info_points
                     (id SERIAL PRIMARY KEY, address TEXT, lat REAL, lon REAL, comment TEXT)''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS zones
                     (id SERIAL PRIMARY KEY, coords TEXT, comment TEXT)''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS departures
                     (id SERIAL PRIMARY KEY, point_id INTEGER UNIQUE, point_title TEXT, person_name TEXT)''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS settings
                     (key TEXT PRIMARY KEY, value TEXT)''')
        
        c.execute("INSERT INTO settings (key, value) VALUES ('hide_empty_labels', 'false') ON CONFLICT (key) DO NOTHING")
        c.execute("INSERT INTO settings (key, value) VALUES ('show_parkings', 'true') ON CONFLICT (key) DO NOTHING")
        c.execute("INSERT INTO settings (key, value) VALUES ('show_labels', 'true') ON CONFLICT (key) DO NOTHING")
        
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
    if message.text is None:
        return 

    text = message.text.strip()
    text_lower = text.lower()

    if text_lower == 'off лишнее':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE settings SET value = 'true' WHERE key = 'hide_empty_labels'")
        conn.commit(); conn.close()
        bot.reply_to(message, "✅ Лишние плашки (со знаком '?') теперь скрыты.")
        return
        
    if text_lower == 'вкл всё':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE settings SET value = 'false' WHERE key = 'hide_empty_labels'")
        conn.commit(); conn.close()
        bot.reply_to(message, "✅ Все плашки теперь видны.")
        return
        
    if text_lower == 'off видимость парковок':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE settings SET value = 'false' WHERE key = 'show_parkings'")
        conn.commit(); conn.close()
        bot.reply_to(message, "✅ Парковки скрыты с карты.")
        return
        
    if text_lower == 'вкл видимость парковок':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE settings SET value = 'true' WHERE key = 'show_parkings'")
        conn.commit(); conn.close()
        bot.reply_to(message, "✅ Парковки отображаются на карте.")
        return
        
    if text_lower == 'off плашки':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE settings SET value = 'false' WHERE key = 'show_labels'")
        conn.commit(); conn.close()
        bot.reply_to(message, "✅ Плашки над парковками скрыты.")
        return
        
    if text_lower == 'вкл плашки':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE settings SET value = 'true' WHERE key = 'show_labels'")
        conn.commit(); conn.close()
        bot.reply_to(message, "✅ Плашки над парковками отображаются.")
        return

    if text_lower == 'сброс парковок':
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("UPDATE parkings SET available_count = '?', dead_count = '?', broken_count = '?'")
            count = c.rowcount
            conn.commit()
            conn.close()
            bot.reply_to(message, f"✅ Статистика всех парковок ({count} шт.) успешно сброшена на '?'")
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка при сбросе: {e}")
        return

    if text_lower.endswith(' off ссылку'):
        name = text[:-11].strip()
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE parkings SET photo_url = NULL WHERE title = %s", (name,))
        conn.commit(); conn.close()
        bot.reply_to(message, f"️ Ссылка на фото для '{name}' удалена.")
        return

    if ' http' in text or text.startswith('http'):
        parts = text.split(' ', 1)
        if len(parts) == 2 and parts[1].startswith('http'):
            name = parts[0].strip()
            photo_url = parts[1].strip()
            conn = get_db_connection(); c = conn.cursor()
            c.execute("UPDATE parkings SET photo_url = %s WHERE title = %s", (photo_url, name))
            conn.commit(); conn.close()
            bot.reply_to(message, f"✅ Ссылка на фото для '{name}' сохранена.")
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
                conn = get_db_connection(); c = conn.cursor()
                c.execute("INSERT INTO parkings (title, address, lat, lon) VALUES (%s, %s, %s, %s)", (title, location_part, lat, lon))
                conn.commit(); conn.close()
                bot.reply_to(message, f"✅ Парковка '{title}' добавлена!")
            else:
                bot.reply_to(message, f"❌ Не найдено: '{location_part}'")
        return

    if ' - ' in text:
        parts = text.split(' - ', 1)
        title = parts[0].strip()
        action = parts[1].strip()
        
        if action.lower() == 'удалить':
            conn = get_db_connection(); c = conn.cursor()
            c.execute("DELETE FROM parkings WHERE title = %s", (title,))
            if c.rowcount > 0:
                bot.reply_to(message, f"🗑️ Парковка '{title}' успешно удалена.")
            else:
                bot.reply_to(message, f"❌ Парковка '{title}' не найдена.")
            conn.commit(); conn.close()
            return
        
        params_match = re.findall(r'([СсДдПп])(\d+|\?)', action)
        if params_match:
            avail = dead = broken = None
            for param_type, param_value in params_match:
                p_type = param_type.lower()
                if p_type == 'с': dead = param_value
                elif p_type == 'д': avail = param_value
                elif p_type == 'п': broken = param_value
            
            conn = get_db_connection(); c = conn.cursor()
            c.execute("SELECT available_count, dead_count, broken_count FROM parkings WHERE title = %s", (title,))
            row = c.fetchone()
            
            if row:
                new_avail = avail if avail is not None else row[0]
                new_dead = dead if dead is not None else row[1]
                new_broken = broken if broken is not None else row[2]
                
                c.execute("UPDATE parkings SET available_count = %s, dead_count = %s, broken_count = %s WHERE title = %s", 
                         (new_avail, new_dead, new_broken, title))
                conn.commit(); conn.close()
                
                response_parts = []
                if avail is not None: response_parts.append(f"Д:{new_avail}")
                if dead is not None: response_parts.append(f"С:{new_dead}")
                if broken is not None: response_parts.append(f"П:{new_broken}")
                
                bot.reply_to(message, f"✅ '{title}' обновлена: {', '.join(response_parts)}")
            else:
                bot.reply_to(message, f"❌ Парковка '{title}' не найдена.")
            return

    if ' - доп инфа - ' in text_lower:
        parts = text.split(' - ', 2)
        if len(parts) == 3:
            location_part = parts[0].strip()
            comment = parts[2].strip()
            
            coord_match = re.match(r'^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$', location_part)
            if coord_match:
                lat, lon = float(coord_match.group(1)), float(coord_match.group(2))
            else:
                bot.reply_to(message, f"⏳ Ищу '{location_part}'...")
                lat, lon = get_coordinates(location_part)
            
            if lat and lon:
                conn = get_db_connection(); c = conn.cursor()
                c.execute("INSERT INTO info_points (address, lat, lon, comment) VALUES (%s, %s, %s, %s)", 
                         (location_part, lat, lon, comment))
                conn.commit(); conn.close()
                bot.reply_to(message, f"✅ Доп инфа добавлена!")
            else:
                bot.reply_to(message, f"❌ Не найдено: '{location_part}'")
        return

    if '-' in text:
        parts = text.rsplit('-', 1)
        location_part = parts[0].strip()
        right_side = parts[1].strip()
        
        coord_match = re.match(r'^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$', location_part)
        if coord_match:
            lat, lon = float(coord_match.group(1)), float(coord_match.group(2))
        else:
            bot.reply_to(message, f"⏳ Ищу '{location_part}'...")
            lat, lon = get_coordinates(location_part)
            
        if lat and lon:
            url_match = re.search(r'(https?://\S+)', right_side)
            url = url_match.group(1) if url_match else ""
            if url:
                right_side = right_side.replace(url, "").strip()
                
            status = 'active'
            if re.search(r'\bred\b', right_side, re.IGNORECASE):
                status = 'red'
                right_side = re.sub(r'\bred\b', '', right_side, flags=re.IGNORECASE).strip()
                
            remaining_parts = right_side.split()
            if len(remaining_parts) > 0:
                title = remaining_parts[0]
                comment = " ".join(remaining_parts[1:]) if len(remaining_parts) > 1 else ""
            else:
                title = "Точка"
                comment = ""
                
            conn = get_db_connection(); c = conn.cursor()
            c.execute("INSERT INTO points (title, address, lat, lon, status, comment, url) VALUES (%s, %s, %s, %s, %s, %s, %s)", 
                     (title, location_part, lat, lon, status, comment, url))
            conn.commit(); conn.close()
            bot.reply_to(message, f"✅ Точка '{title}' добавлена!")
        else:
            bot.reply_to(message, f"❌ Не найдено: '{location_part}'")
        return

    if text_lower == 'все p off':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("DELETE FROM parkings")
        count = c.rowcount
        conn.commit(); conn.close()
        bot.reply_to(message, f"️ Удалено всех парковок: {count}")
        return

    if text_lower == 'все с off':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("DELETE FROM points")
        count = c.rowcount
        c.execute("DELETE FROM departures")
        conn.commit(); conn.close()
        bot.reply_to(message, f"️ Удалено всех самокатов: {count}")
        return

    if text_lower == 'last p off':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("DELETE FROM parkings WHERE id = (SELECT MAX(id) FROM parkings)")
        if c.rowcount > 0:
            bot.reply_to(message, "🗑️ Последняя парковка успешно удалена.")
        else:
            bot.reply_to(message, "❌ Парковок не найдено.")
        conn.commit(); conn.close()
        return

# --- API ЭНДПОИНТЫ ---

@app.route('/get_settings')
def get_settings():
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT key, value FROM settings")
    settings = {row[0]: row[1] == 'true' for row in c.fetchall()}
    conn.close()
    return jsonify(settings)

@app.route('/update_setting', methods=['POST'])
def update_setting():
    data = request.json
    conn = get_db_connection(); c = conn.cursor()
    c.execute("UPDATE settings SET value = %s WHERE key = %s", (str(data.get('value')).lower(), data.get('key')))
    conn.commit(); conn.close()
    return jsonify({"success": True})

@app.route('/get_points')
def get_points():
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT id, title, address, lat, lon, status, comment, url FROM points WHERE status != 'found'")
    points = [{"id": r[0], "title": r[1], "address": r[2], "lat": r[3], "lon": r[4], "status": r[5], "comment": r[6] or "", "url": r[7] or ""} for r in c.fetchall()]
    conn.close()
    return jsonify(points)

@app.route('/get_info_points')
def get_info_points():
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT id, address, lat, lon, comment FROM info_points")
    info_points = [{"id": r[0], "address": r[1], "lat": r[2], "lon": r[3], "comment": r[4] or ""} for r in c.fetchall()]
    conn.close()
    return jsonify(info_points)

@app.route('/add_info_point', methods=['POST'])
def add_info_point():
    try:
        data = request.json
        conn = get_db_connection(); c = conn.cursor()
        c.execute("INSERT INTO info_points (address, lat, lon, comment) VALUES (%s, %s, %s, %s)", 
                 (data.get('address', ''), data.get('lat'), data.get('lon'), data.get('comment', '')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/delete_info_point', methods=['POST'])
def delete_info_point():
    try:
        data = request.json
        conn = get_db_connection(); c = conn.cursor()
        c.execute("DELETE FROM info_points WHERE id = %s", (data.get('id'),))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/update_info_comment', methods=['POST'])
def update_info_comment():
    try:
        data = request.json
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE info_points SET comment = %s WHERE id = %s", 
                 (data.get('comment', ''), data.get('id')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

# ✅ НОВЫЙ ЭНДПОИНТ: Создание самоката через правый клик
@app.route('/create_scooter', methods=['POST'])
def create_scooter():
    try:
        data = request.json
        conn = get_db_connection(); c = conn.cursor()
        c.execute("INSERT INTO points (title, address, lat, lon, status, comment) VALUES (%s, %s, %s, %s, %s, %s)", 
                 (data.get('number', ''), data.get('address', ''), data.get('lat'), data.get('lon'), data.get('status', 'active'), data.get('comment', '')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

# ✅ НОВЫЙ ЭНДПОИНТ: Создание парковки через правый клик
@app.route('/create_parking', methods=['POST'])
def create_parking():
    try:
        data = request.json
        conn = get_db_connection(); c = conn.cursor()
        c.execute("INSERT INTO parkings (title, address, lat, lon) VALUES (%s, %s, %s, %s)", 
                 (data.get('title', ''), data.get('address', ''), data.get('lat'), data.get('lon')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/update_status', methods=['POST'])
def update_status():
    data = request.json
    conn = get_db_connection(); c = conn.cursor()
    if data.get('status') == 'found':
        c.execute("DELETE FROM points WHERE id = %s", (data.get('id'),))
    else:
        c.execute("UPDATE points SET status = %s WHERE id = %s", (data.get('status'), data.get('id')))
    conn.commit(); conn.close()
    return jsonify({"success": True})

@app.route('/update_comment', methods=['POST'])
def update_comment():
    try:
        data = request.json
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE points SET comment = %s WHERE id = %s", (data.get('comment', ''), data.get('point_id')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/get_parkings')
def get_parkings():
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT id, title, address, lat, lon, available_count, dead_count, broken_count, photo_url FROM parkings")
    parkings = [{"id": r[0], "title": r[1] or "?", "address": r[2] or "?", "lat": float(r[3]), "lon": float(r[4]), 
                 "available": r[5] or '?', "dead": r[6] or '?', "broken": r[7] or '?', "photo_url": r[8] or ""} for r in c.fetchall() if r[3] and r[4]]
    conn.close()
    return jsonify(parkings)

@app.route('/update_parking', methods=['POST'])
def update_parking():
    data = request.json
    conn = get_db_connection(); c = conn.cursor()
    c.execute("UPDATE parkings SET available_count = %s, dead_count = %s, broken_count = %s WHERE id = %s", 
              (data.get('available', '?'), data.get('dead', '?'), data.get('broken', '?'), data.get('id')))
    conn.commit(); conn.close()
    return jsonify({"success": True})

@app.route('/get_zones', methods=['GET'])
def get_zones():
    conn = get_db_connection(); c = conn.cursor()
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
    conn = get_db_connection(); c = conn.cursor()
    c.execute("INSERT INTO zones (coords, comment) VALUES (%s, %s)", (json.dumps(data.get('coords')), data.get('comment', '')))
    conn.commit(); conn.close()
    return jsonify({"success": True})

@app.route('/delete_zone', methods=['POST'])
def delete_zone():
    data = request.json
    conn = get_db_connection(); c = conn.cursor()
    c.execute("DELETE FROM zones WHERE id = %s", (data.get('id'),))
    conn.commit(); conn.close()
    return jsonify({"success": True})

@app.route('/get_departures')
def get_departures():
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT point_id, point_title, person_name FROM departures")
    departures = [{"point_id": r[0], "point_title": r[1], "person_name": r[2]} for r in c.fetchall()]
    conn.close()
    return jsonify(departures)

@app.route('/add_departure', methods=['POST'])
def add_departure():
    data = request.json
    conn = get_db_connection(); c = conn.cursor()
    c.execute("""INSERT INTO departures (point_id, point_title, person_name) VALUES (%s, %s, %s)
                 ON CONFLICT (point_id) DO UPDATE SET point_title = EXCLUDED.point_title, person_name = EXCLUDED.person_name""", 
              (data.get('point_id'), data.get('point_title'), data.get('person_name')))
    conn.commit(); conn.close()
    return jsonify({"success": True})

@app.route('/remove_departure', methods=['POST'])
def remove_departure():
    data = request.json
    conn = get_db_connection(); c = conn.cursor()
    c.execute("DELETE FROM departures WHERE point_id = %s", (data.get('point_id'),))
    conn.commit(); conn.close()
    return jsonify({"success": True})

def run_bot():
    bot.polling(none_stop=True)

if __name__ == '__main__':
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    app.run(host='0.0.0.0', port=8080)
