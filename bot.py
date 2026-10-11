import telebot
import requests
import psycopg
import re
from flask import Flask, jsonify, request
from flask_cors import CORS
import threading
import os
import traceback

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8803648566:AAHmG4XTMTDqfIHlWjBeDsKCGmQ18pxKnGQ")
ADMIN_CHAT_ID = 7929131842
# Используем переменную окружения Render, а если её нет — берем из строки
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://mapbotuser:r47l5ou0pDueVnus4tiD3d2w7hYQJ1vy@dpg-db4fc5ks728c73ajgig0-a.frankfurt-postgres.render.com/mapbotdb")

app = Flask(__name__)
CORS(app)

def get_db_connection():
    return psycopg.connect(DATABASE_URL)

def init_db():
    try:
        print("🔄 Подключение к PostgreSQL...")
        conn = get_db_connection()
        c = conn.cursor()
        
        print("🔄 Создание таблицы points...")
        c.execute('''CREATE TABLE IF NOT EXISTS points
                     (id SERIAL PRIMARY KEY,
                      title TEXT, address TEXT, lat REAL, lon REAL, status TEXT DEFAULT 'active')''')
        
        print("🔄 Пересоздание таблицы parkings...")
        c.execute("DROP TABLE IF EXISTS parkings")
        c.execute('''CREATE TABLE parkings
                     (id SERIAL PRIMARY KEY,
                      title TEXT UNIQUE, address TEXT, lat REAL, lon REAL, 
                      available_count TEXT DEFAULT '?', dead_count TEXT DEFAULT '?', broken_count TEXT DEFAULT '?')''')
        
        conn.commit()
        conn.close()
        print("✅ База данных успешно инициализирована!")
    except Exception as e:
        print(f"❌ Ошибка инициализации БД: {e}")
        traceback.print_exc()

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
            count = c.rowcount
            conn.commit()
            conn.close()
            bot.reply_to(message, f"🗑️ Удалено всех парковок: {count}")
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {e}")
        return

    if text_lower == 'last p off':
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("DELETE FROM parkings WHERE id = (SELECT MAX(id) FROM parkings)")
            if c.rowcount > 0:
                bot.reply_to(message, "🗑️ Последняя парковка успешно удалена.")
            else:
                bot.reply_to(message, "❌ Парковок не найдено.")
            conn.commit()
            conn.close()
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {e}")
        return

    parking_match = re.match(r'^(.+?)\s+([ДдСсПп]\d+(?:\s+[ДдСсПп]\d+)*)$', text)
    if parking_match:
        title = parking_match.group(1).strip()
        params_str = parking_match.group(2)
        
        avail = dead = broken = None
        params = re.findall(r'([ДдСсПп])(\d+)', params_str)
        for param_type, param_value in params:
            if param_type.lower() == 'д': avail = param_value
            elif param_type.lower() == 'с': dead = param_value
            elif param_type.lower() == 'п': broken = param_value
        
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT available_count, dead_count, broken_count FROM parkings WHERE title = %s", (title,))
            row = c.fetchone()
            
            if row:
                current_avail, current_dead, current_broken = row
                new_avail = avail if avail is not None else current_avail
                new_dead = dead if dead is not None else current_dead
                new_broken = broken if broken is not None else current_broken
                
                c.execute("UPDATE parkings SET available_count = %s, dead_count = %s, broken_count = %s WHERE title = %s", 
                         (new_avail, new_dead, new_broken, title))
                conn.commit()
                
                response_parts = []
                if avail is not None: response_parts.append(f"В доступе: {avail}")
                if dead is not None: response_parts.append(f"Севшие: {dead}")
                if broken is not None: response_parts.append(f"Поломки: {broken}")
                
                bot.reply_to(message, f"✅ Парковка \"{title}\" обновлена: {', '.join(response_parts)}")
            else:
                bot.reply_to(message, f"❌ Парковка \"{title}\" не найдена в базе данных.")
            conn.close()
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка БД: {e}")
        return

    if '-' in text:
        parts = text.rsplit('-', 1) 
        location_part = parts[0].strip()
        title = parts[1].strip()
        
        if not location_part or not title:
            bot.reply_to(message, "⚠️ Формат: Адрес или Координаты - Название")
            return

        coord_match = re.match(r'^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$', location_part)
        if coord_match:
            lat, lon = float(coord_match.group(1)), float(coord_match.group(2))
        else:
            bot.reply_to(message, f"⏳ Ищу '{location_part}' в г. Смоленск...")
            lat, lon = get_coordinates(location_part)
            
        if lat and lon:
            try:
                conn = get_db_connection()
                c = conn.cursor()
                c.execute("INSERT INTO points (title, address, lat, lon, status) VALUES (%s, %s, %s, %s, 'active')",
                         (title, location_part, lat, lon))
                conn.commit()
                conn.close()
                bot.reply_to(message, f"✅ Точка '{title}' успешно добавлена!")
            except Exception as e:
                bot.reply_to(message, f"❌ Ошибка БД: {e}")
        else:
            bot.reply_to(message, f"❌ Не удалось найти: '{location_part}' в Смоленске.")

# --- API ЭНДПОИНТЫ ---

@app.route('/')
def serve_website():
    return "Бот работает! API доступно."

@app.route('/get_points')
def get_points():
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, title, address, lat, lon, status FROM points WHERE status != 'found'")
        points = [{"id": row[0], "title": row[1], "address": row[2], "lat": row[3], "lon": row[4], "status": row[5]} for row in c.fetchall()]
        conn.close()
        return jsonify(points)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/update_status', methods=['POST'])
def update_status():
    try:
        data = request.json
        point_id = data.get('id')
        status = data.get('status')
        conn = get_db_connection()
        c = conn.cursor()
        if status == 'found':
            c.execute("DELETE FROM points WHERE id = %s", (point_id,))
        else:
            c.execute("UPDATE points SET status = %s WHERE id = %s", (status, point_id))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ✅ ОТЛАДОЧНЫЙ ЭНДПОИНТ: покажет точную ошибку прямо в браузере!
@app.route('/get_parkings')
def get_parkings():
    try:
        print("🔄 Запрос /get_parkings получен")
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, title, address, lat, lon, available_count, dead_count, broken_count FROM parkings")
        parkings = []
        for row in c.fetchall():
            if row[3] is None or row[4] is None: 
                continue
            parkings.append({
                "id": row[0], 
                "title": row[1] or "Без названия", 
                "address": row[2] or "?", 
                "lat": float(row[3]), 
                "lon": float(row[4]), 
                "available": row[5] if row[5] is not None else '?', 
                "dead": row[6] if row[6] is not None else '?', 
                "broken": row[7] if row[7] is not None else '?'
            })
        conn.close()
        print(f"✅ Успешно возвращено {len(parkings)} парковок")
        return jsonify(parkings)
    except Exception as e:
        error_msg = str(e)
        print(f"❌ Ошибка в /get_parkings: {error_msg}")
        traceback.print_exc()
        # Возвращаем ошибку прямо в браузер, чтобы ты видел, что сломалось!
        return jsonify({"error": "Внутренняя ошибка сервера", "details": error_msg}), 500

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

def run_bot():
    bot.polling(none_stop=True)

if __name__ == '__main__':
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
