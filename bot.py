import telebot
import requests
import psycopg
import json
import re
from flask import Flask, jsonify, request
from flask_cors import CORS
import threading
import os
import traceback
import time
import signal
import sys
from datetime import datetime

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8803648566:AAEHSBq4MvzNwbyks2-1BgeRBweQuvg7f6E")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://mapbotuser:r47l5ou0pDueVnus4tiD3d2w7hYQJ1vy@dpg-db4fc5ks728c73ajgig0-a.frankfurt-postgres.render.com/mapbotdb")

app = Flask(__name__)
CORS(app)

db_initialized = False
bot_running = False

# ✅ ХРАНИЛИЩЕ МОНИТОРИНГОВ
active_monitors = {}

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
    try:
        return psycopg.connect(DATABASE_URL)
    except Exception as e:
        print(f" Ошибка подключения к БД: {e}")
        return None

def column_exists(c, table, column):
    try:
        c.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name='{table}' AND column_name='{column}'")
        return c.fetchone() is not None
    except:
        return False

def init_db():
    global db_initialized
    if db_initialized:
        return
    print(" Начинаю инициализацию БД...")
    try:
        conn = get_db_connection()
        if not conn:
            print("❌ Не удалось подключиться к БД")
            return
        c = conn.cursor()
        print("📦 Создаю таблицы...")
        c.execute('''CREATE TABLE IF NOT EXISTS points
                     (id SERIAL PRIMARY KEY, title TEXT, address TEXT, lat REAL, lon REAL, 
                      status TEXT DEFAULT 'active', comment TEXT, url TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS parkings
                     (id SERIAL PRIMARY KEY, title TEXT UNIQUE, address TEXT, lat REAL, lon REAL, 
                      available_count TEXT DEFAULT '?', dead_count TEXT DEFAULT '?', 
                      broken_count TEXT DEFAULT '?', photo_url TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS zones
                     (id SERIAL PRIMARY KEY, coords TEXT, comment TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS routes
                     (id SERIAL PRIMARY KEY, coords TEXT, comment TEXT, temporary BOOLEAN DEFAULT FALSE)''')
        c.execute('''CREATE TABLE IF NOT EXISTS departures
                     (id SERIAL PRIMARY KEY, point_id INTEGER UNIQUE, point_title TEXT, person_name TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS settings
                     (key TEXT PRIMARY KEY, value TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS trash
                     (id SERIAL PRIMARY KEY, type TEXT, data JSON, deleted_at TIMESTAMP DEFAULT NOW())''')
        conn.commit()
        print("✅ Таблицы созданы")
        print(" Проверяю колонки...")
        if not column_exists(c, 'points', 'comment'):
            c.execute("ALTER TABLE points ADD COLUMN comment TEXT"); conn.commit()
        if not column_exists(c, 'points', 'url'):
            c.execute("ALTER TABLE points ADD COLUMN url TEXT"); conn.commit()
        if not column_exists(c, 'points', 'assigned_to'):
            c.execute("ALTER TABLE points ADD COLUMN assigned_to TEXT"); conn.commit()
        if not column_exists(c, 'parkings', 'photo_url'):
            c.execute("ALTER TABLE parkings ADD COLUMN photo_url TEXT"); conn.commit()
        if not column_exists(c, 'zones', 'assigned_to'):
            c.execute("ALTER TABLE zones ADD COLUMN assigned_to TEXT"); conn.commit()
        if not column_exists(c, 'routes', 'temporary'):
            c.execute("ALTER TABLE routes ADD COLUMN temporary BOOLEAN DEFAULT FALSE"); conn.commit()
        if not column_exists(c, 'routes', 'assigned_to'):
            c.execute("ALTER TABLE routes ADD COLUMN assigned_to TEXT"); conn.commit()
        settings = [('hide_empty_labels', 'false'), ('show_parkings', 'true'), ('show_labels', 'true')]
        for key, value in settings:
            c.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (key, value))
        conn.commit()
        db_initialized = True
        print("✅ База данных полностью инициализирована!")
    except Exception as e:
        print(f"❌ Ошибка инициализации БД: {e}")
        traceback.print_exc()

@app.before_request
def ensure_db_initialized():
    init_db()

def save_to_trash(obj_type, data):
    try:
        conn = get_db_connection()
        if not conn: return
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM trash WHERE type = %s", (obj_type,))
        count = c.fetchone()[0]
        if count >= 5:
            c.execute("""DELETE FROM trash WHERE id IN 
                        (SELECT id FROM trash WHERE type = %s ORDER BY deleted_at ASC LIMIT 1)""", (obj_type,))
        c.execute("INSERT INTO trash (type, data) VALUES (%s, %s)", 
                 (obj_type, json.dumps(data, ensure_ascii=False, default=str)))
        conn.commit(); conn.close()
        print(f"✅ Сохранено в trash: {obj_type}")
    except Exception as e:
        print(f"❌ Ошибка сохранения в trash: {e}")

def restore_from_trash():
    try:
        conn = get_db_connection()
        if not conn: return None, "Нет подключения к БД"
        c = conn.cursor()
        c.execute("SELECT id, type, data FROM trash ORDER BY deleted_at DESC LIMIT 1")
        row = c.fetchone()
        if not row:
            conn.close()
            return None, "Корзина пуста"
        trash_id, obj_type, data = row
        data = json.loads(data) if isinstance(data, str) else data
        if obj_type in ('point', 'info'):
            c.execute("""INSERT INTO points (title, address, lat, lon, status, comment, url, assigned_to)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                     (data.get('title'), data.get('address'), data.get('lat'), data.get('lon'),
                      data.get('status', 'active'), data.get('comment', ''), data.get('url', ''),
                      data.get('assigned_to')))
            new_id = c.fetchone()[0]
            c.execute("DELETE FROM trash WHERE id = %s", (trash_id,))
            conn.commit(); conn.close()
            return f"✅ Восстановлен объект '{data.get('title')}' (ID: {new_id})", None
        elif obj_type == 'zone':
            c.execute("""INSERT INTO zones (coords, comment, assigned_to)
                        VALUES (%s, %s, %s) RETURNING id""",
                     (data.get('coords'), data.get('comment', ''), data.get('assigned_to')))
            new_id = c.fetchone()[0]
            c.execute("DELETE FROM trash WHERE id = %s", (trash_id,))
            conn.commit(); conn.close()
            return f"✅ Восстановлена зона (ID: {new_id})", None
        elif obj_type == 'parking':
            c.execute("""INSERT INTO parkings (title, address, lat, lon, available_count, dead_count, broken_count, photo_url)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                     (data.get('title'), data.get('address'), data.get('lat'), data.get('lon'),
                      data.get('available_count', '?'), data.get('dead_count', '?'),
                      data.get('broken_count', '?'), data.get('photo_url')))
            new_id = c.fetchone()[0]
            c.execute("DELETE FROM trash WHERE id = %s", (trash_id,))
            conn.commit(); conn.close()
            return f"✅ Восстановлена парковка '{data.get('title')}' (ID: {new_id})", None
        else:
            conn.close()
            return None, f"Неизвестный тип: {obj_type}"
    except Exception as e:
        print(f"❌ Ошибка восстановления: {e}")
        traceback.print_exc()
        return None, str(e)

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

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

# ✅ КОМАНДЫ МОНИТОРИНГА

@bot.message_handler(commands=['мониторь'])
def start_monitor(message):
    """Команда: /мониторь 775"""
    if message.chat.type not in ['group', 'supergroup']:
        bot.reply_to(message, "Эта команда работает только в группах!")
        return
    
    text = message.text.strip()
    parts = text.split()
    
    if len(parts) < 2:
        bot.reply_to(message, "Использование: /мониторь <последние_3_цифры>\nПример: /мониторь 775")
        return
    
    last_three = parts[1]
    
    if not last_three.isdigit() or len(last_three) != 3:
        bot.reply_to(message, "Нужно ровно 3 цифры!\nПример: /мониторь 775")
        return
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    start_time = datetime.now()
    
    monitor_key = f"{chat_id}_{last_three}"
    active_monitors[monitor_key] = {
        'chat_id': chat_id,
        'last_three': last_three,
        'user_id': user_id,
        'start_time': start_time,
        'active': True
    }
    
    bot.reply_to(message, 
        f"✅ Мониторинг запущен!\n\n"
        f"🔍 Ищу: 7703{last_three}\n"
        f"⏰ С: {start_time.strftime('%d.%m.%Y %H:%M:%S')}\n"
        f" Уведомления в ЛС")


@bot.message_handler(commands=['стоп'])
def stop_monitor(message):
    """Команда: /стоп 775"""
    text = message.text.strip()
    parts = text.split()
    
    if len(parts) < 2:
        bot.reply_to(message, "Использование: /стоп <последние_3_цифры>")
        return
    
    last_three = parts[1]
    chat_id = message.chat.id
    monitor_key = f"{chat_id}_{last_three}"
    
    if monitor_key in active_monitors:
        del active_monitors[monitor_key]
        bot.reply_to(message, f"❌ Мониторинг 7703{last_three} остановлен")
    else:
        bot.reply_to(message, "Мониторинг не найден")


@bot.message_handler(commands=['мониторы'])
def list_monitors(message):
    """Показать активные мониторинги"""
    chat_id = message.chat.id
    user_monitors = {k: v for k, v in active_monitors.items() 
                     if v['chat_id'] == chat_id and v['user_id'] == message.from_user.id and v['active']}
    
    if not user_monitors:
        bot.reply_to(message, "Нет активных мониторингов")
        return
    
    text = "🔍 Активные мониторинги:\n\n"
    for key, monitor in user_monitors.items():
        text += f"• 7703{monitor['last_three']}\n  С {monitor['start_time'].strftime('%d.%m.%Y %H:%M')}\n\n"
    
    bot.reply_to(message, text)


# ✅ ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if message.text is None:
        return
    text = message.text.strip()
    text_lower = text.lower()

    # ✅ МОНИТОРИНГ - проверяем в самом начале
    if message.chat.type in ['group', 'supergroup'] and message.text:
        message_time = datetime.fromtimestamp(message.date)
        
        for key, monitor in list(active_monitors.items()):
            if monitor['chat_id'] == message.chat.id and monitor['active']:
                # Пропускаем сообщения до начала мониторинга
                if message_time < monitor['start_time']:
                    continue
                
                # Ищем паттерн 7703XXX
                pattern = f"7703{monitor['last_three']}"
                if pattern in message.text:
                    try:
                        # ✅ Отправляем в ЛС пользователю
                        bot.send_message(
                            monitor['user_id'],
                            f"Произошло новое ФЗ {monitor['last_three']}!"
                        )
                        print(f"✅ Найдено: {pattern} в группе {message.chat.id}, отправлено пользователю {monitor['user_id']}")
                    except Exception as e:
                        print(f"Ошибка отправки уведомления: {e}")

    # ✅ КОМАНДА ОТМЕНЫ УДАЛЕНИЯ
    if text_lower == 'z':
        result, error = restore_from_trash()
        if error:
            bot.reply_to(message, f" {error}")
        else:
            bot.reply_to(message, result)
        return

    # ✅ ПРОСМОТР КОРЗИНЫ
    if text_lower in ('trash', 'корзина'):
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT type, data, deleted_at FROM trash ORDER BY deleted_at DESC")
            items = c.fetchall()
            conn.close()
            if not items:
                bot.reply_to(message, "🗑️ Корзина пуста")
            else:
                msg = "📦 Корзина (последние удаления):\n\n"
                for i, (obj_type, data, deleted_at) in enumerate(items, 1):
                    data = json.loads(data) if isinstance(data, str) else data
                    if obj_type == 'point':
                        msg += f"{i}. 🛴 Самокат '{data.get('title', '')}'\n"
                    elif obj_type == 'info':
                        msg += f"{i}. ℹ️ Доп инфа '{data.get('title', '')}'\n"
                    elif obj_type == 'zone':
                        msg += f"{i}. 🔴 Зона\n"
                    elif obj_type == 'parking':
                        msg += f"{i}. 🅿️ Парковка '{data.get('title', '')}'\n"
                msg += "\nНапиши Z чтобы восстановить последнее"
                bot.reply_to(message, msg)
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {e}")
        return

    # ✅ УДАЛЕНИЕ МАРШРУТОВ
    if text_lower == 'off все маршруты':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("DELETE FROM routes")
        count = c.rowcount
        conn.commit(); conn.close()
        bot.reply_to(message, f"🗑️ Удалено всех маршрутов: {count}")
        return
    
    if text_lower == 'off last маршрут':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("DELETE FROM routes WHERE id = (SELECT MAX(id) FROM routes)")
        if c.rowcount > 0:
            bot.reply_to(message, "️ Последний маршрут удалён.")
        else:
            bot.reply_to(message, "❌ Маршрутов нет.")
        conn.commit(); conn.close()
        return

    if text_lower == 'off лишнее':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE settings SET value = 'true' WHERE key = 'hide_empty_labels'")
        conn.commit(); conn.close()
        bot.reply_to(message, "✅ Лишние плашки скрыты.")
        return
    if text_lower == 'вкл всё':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE settings SET value = 'false' WHERE key = 'hide_empty_labels'")
        conn.commit(); conn.close()
        bot.reply_to(message, "✅ Все плашки видны.")
        return
    if text_lower == 'off видимость парковок':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE settings SET value = 'false' WHERE key = 'show_parkings'")
        conn.commit(); conn.close()
        bot.reply_to(message, "✅ Парковки скрыты.")
        return
    if text_lower == 'вкл видимость парковок':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE settings SET value = 'true' WHERE key = 'show_parkings'")
        conn.commit(); conn.close()
        bot.reply_to(message, "✅ Парковки видны.")
        return
    if text_lower == 'off плашки':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE settings SET value = 'false' WHERE key = 'show_labels'")
        conn.commit(); conn.close()
        bot.reply_to(message, "✅ Плашки скрыты.")
        return
    if text_lower == 'вкл плашки':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE settings SET value = 'true' WHERE key = 'show_labels'")
        conn.commit(); conn.close()
        bot.reply_to(message, "✅ Плашки видны.")
        return
    if text_lower == 'сброс парковок':
        try:
            conn = get_db_connection(); c = conn.cursor()
            c.execute("UPDATE parkings SET available_count = '?', dead_count = '?', broken_count = '?'")
            count = c.rowcount
            conn.commit(); conn.close()
            bot.reply_to(message, f"✅ Статистика сброшена ({count} шт.)")
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {e}")
        return

    if text_lower.endswith(' off ссылку'):
        name = text[:-11].strip()
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE parkings SET photo_url = NULL WHERE title = %s", (name,))
        conn.commit(); conn.close()
        bot.reply_to(message, f"🗑️ Ссылка удалена.")
        return
    if ' http' in text or text.startswith('http'):
        parts = text.split(' ', 1)
        if len(parts) == 2 and parts[1].startswith('http'):
            name = parts[0].strip()
            photo_url = parts[1].strip()
            conn = get_db_connection(); c = conn.cursor()
            c.execute("UPDATE parkings SET photo_url = %s WHERE title = %s", (photo_url, name))
            conn.commit(); conn.close()
            bot.reply_to(message, f"✅ Ссылка сохранена.")
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
            c.execute("SELECT id, title, address, lat, lon, available_count, dead_count, broken_count, photo_url FROM parkings WHERE title = %s", (title,))
            row = c.fetchone()
            if row:
                parking_data = {'id': row[0], 'title': row[1], 'address': row[2], 'lat': row[3], 'lon': row[4],
                    'available_count': row[5], 'dead_count': row[6], 'broken_count': row[7], 'photo_url': row[8]}
                save_to_trash('parking', parking_data)
            c.execute("DELETE FROM parkings WHERE title = %s", (title,))
            if c.rowcount > 0:
                bot.reply_to(message, f"🗑️ Парковка '{title}' удалена.")
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
                bot.reply_to(message, f" Парковка '{title}' не найдена.")
            return

    if text_lower.endswith(' - доп инфа'):
        clean_text = text[:-10].strip()
        if ' - ' in clean_text:
            parts = clean_text.split(' - ', 1)
            location_part = parts[0].strip()
            comment = parts[1].strip()
        else:
            location_part = clean_text
            comment = ""
        coord_match = re.match(r'^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$', location_part)
        if coord_match:
            lat, lon = float(coord_match.group(1)), float(coord_match.group(2))
        else:
            bot.reply_to(message, f"⏳ Ищу '{location_part}'...")
            lat, lon = get_coordinates(location_part)
        if lat and lon:
            conn = get_db_connection(); c = conn.cursor()
            c.execute("INSERT INTO points (title, address, lat, lon, status, comment) VALUES (%s, %s, %s, %s, 'info', %s)", 
                     ("ДОП ИНФА", location_part, lat, lon, comment))
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
            bot.reply_to(message, f" Ищу '{location_part}'...")
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
            elif re.search(r'\bsev\b', right_side, re.IGNORECASE):
                status = 'sev'
                right_side = re.sub(r'\bsev\b', '', right_side, flags=re.IGNORECASE).strip()
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
        bot.reply_to(message, f"🗑️ Удалено парковок: {count}")
        return
    if text_lower == 'все с off':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("DELETE FROM points")
        count = c.rowcount
        c.execute("DELETE FROM departures")
        conn.commit(); conn.close()
        bot.reply_to(message, f"🗑️ Удалено точек: {count}")
        return
    if text_lower == 'last p off':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("DELETE FROM parkings WHERE id = (SELECT MAX(id) FROM parkings)")
        if c.rowcount > 0:
            bot.reply_to(message, "🗑️ Последняя парковка удалена.")
        else:
            bot.reply_to(message, "❌ Парковок нет.")
        conn.commit(); conn.close()
        return

# --- API ЭНДПОИНТЫ ---

@app.route('/get_settings')
def get_settings():
    try:
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("SELECT key, value FROM settings")
        settings = {row[0]: row[1] == 'true' for row in c.fetchall()}
        conn.close()
        return jsonify(settings)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/update_setting', methods=['POST'])
def update_setting():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("UPDATE settings SET value = %s WHERE key = %s", (str(data.get('value')).lower(), data.get('key')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/get_points')
def get_points():
    try:
        user = request.args.get('user', '')
        conn = get_db_connection()
        if not conn: return jsonify([]), 200
        c = conn.cursor()
        if user:
            c.execute("SELECT id, title, address, lat, lon, status, comment, url, assigned_to FROM points WHERE status != 'found' AND assigned_to = %s", (user,))
        else:
            c.execute("SELECT id, title, address, lat, lon, status, comment, url, assigned_to FROM points WHERE status != 'found'")
        points = [{"id": r[0], "title": r[1], "address": r[2], "lat": float(r[3]) if r[3] else 0, "lon": float(r[4]) if r[4] else 0, "status": r[5], "comment": r[6] or "", "url": r[7] or "", "assigned_to": r[8]} for r in c.fetchall()]
        conn.close()
        print(f"✅ /get_points: возвращено {len(points)} точек")
        return jsonify(points)
    except Exception as e:
        print(f"❌ /get_points: {e}")
        traceback.print_exc()
        return jsonify([]), 200

@app.route('/create_scooter', methods=['POST'])
def create_scooter():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("INSERT INTO points (title, address, lat, lon, status, comment) VALUES (%s, %s, %s, %s, %s, %s)", 
                 (data.get('number', ''), data.get('address', ''), data.get('lat'), data.get('lon'), data.get('status', 'active'), data.get('comment', '')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/create_parking', methods=['POST'])
def create_parking():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("INSERT INTO parkings (title, address, lat, lon) VALUES (%s, %s, %s, %s)", 
                 (data.get('title', ''), data.get('address', ''), data.get('lat'), data.get('lon')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/update_status', methods=['POST'])
def update_status():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        if data.get('status') == 'found':
            c.execute("SELECT id, title, address, lat, lon, status, comment, url, assigned_to FROM points WHERE id = %s", (data.get('id'),))
            row = c.fetchone()
            if row:
                point_data = {'id': row[0], 'title': row[1], 'address': row[2], 'lat': row[3], 'lon': row[4],
                    'status': row[5], 'comment': row[6], 'url': row[7], 'assigned_to': row[8]}
                obj_type = 'info' if row[5] == 'info' else 'point'
                save_to_trash(obj_type, point_data)
                print(f"🗑️ Удалён {obj_type} ID {row[0]}, сохранён в trash")
            c.execute("DELETE FROM points WHERE id = %s", (data.get('id'),))
        else:
            c.execute("UPDATE points SET status = %s WHERE id = %s", (data.get('status'), data.get('id')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/update_comment', methods=['POST'])
def update_comment():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("UPDATE points SET comment = %s WHERE id = %s", (data.get('comment', ''), data.get('point_id')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/update_point_location', methods=['POST'])
def update_point_location():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("UPDATE points SET lat = %s, lon = %s, address = %s WHERE id = %s", 
                 (data.get('lat'), data.get('lon'), data.get('address', ''), data.get('id')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/update_parking_location', methods=['POST'])
def update_parking_location():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("UPDATE parkings SET lat = %s, lon = %s, address = %s WHERE id = %s", 
                 (data.get('lat'), data.get('lon'), data.get('address', ''), data.get('id')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/get_parkings')
def get_parkings():
    try:
        conn = get_db_connection()
        if not conn: return jsonify([]), 200
        c = conn.cursor()
        c.execute("SELECT id, title, address, lat, lon, available_count, dead_count, broken_count, photo_url FROM parkings")
        parkings = []
        for r in c.fetchall():
            if r[3] and r[4]:
                parkings.append({
                    "id": r[0], "title": r[1] or "?", "address": r[2] or "?", 
                    "lat": float(r[3]), "lon": float(r[4]), 
                    "available": r[5] or '?', "dead": r[6] or '?', 
                    "broken": r[7] or '?', "photo_url": r[8] or ""
                })
        conn.close()
        print(f"✅ /get_parkings: возвращено {len(parkings)} парковок")
        return jsonify(parkings)
    except Exception as e:
        print(f"❌ /get_parkings: {e}")
        traceback.print_exc()
        return jsonify([]), 200

@app.route('/update_parking', methods=['POST'])
def update_parking():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("UPDATE parkings SET available_count = %s, dead_count = %s, broken_count = %s WHERE id = %s", 
                  (data.get('available', '?'), data.get('dead', '?'), data.get('broken', '?'), data.get('id')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/delete_parking', methods=['POST'])
def delete_parking():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("SELECT id, title, address, lat, lon, available_count, dead_count, broken_count, photo_url FROM parkings WHERE id = %s", (data.get('id'),))
        row = c.fetchone()
        if row:
            parking_data = {'id': row[0], 'title': row[1], 'address': row[2], 'lat': row[3], 'lon': row[4],
                'available_count': row[5], 'dead_count': row[6], 'broken_count': row[7], 'photo_url': row[8]}
            save_to_trash('parking', parking_data)
            print(f"🗑️ Удалена парковка ID {row[0]}, сохранена в trash")
        c.execute("DELETE FROM parkings WHERE id = %s", (data.get('id'),))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/get_zones')
def get_zones():
    try:
        user = request.args.get('user', '')
        conn = get_db_connection()
        if not conn: return jsonify([]), 200
        c = conn.cursor()
        if user:
            c.execute("SELECT id, coords, comment, assigned_to FROM zones WHERE assigned_to = %s", (user,))
        else:
            c.execute("SELECT id, coords, comment, assigned_to FROM zones")
        zones = []
        for r in c.fetchall():
            try:
                coords_list = json.loads(r[1]) if r[1] else []
                zones.append({"id": r[0], "coords": coords_list, "comment": r[2] or "", "assigned_to": r[3]})
            except:
                continue
        conn.close()
        print(f"✅ /get_zones: возвращено {len(zones)} зон")
        return jsonify(zones)
    except Exception as e:
        print(f"❌ /get_zones: {e}")
        traceback.print_exc()
        return jsonify([]), 200

@app.route('/add_zone', methods=['POST'])
def add_zone():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("INSERT INTO zones (coords, comment) VALUES (%s, %s)", (json.dumps(data.get('coords')), data.get('comment', '')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/delete_zone', methods=['POST'])
def delete_zone():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("SELECT id, coords, comment, assigned_to FROM zones WHERE id = %s", (data.get('id'),))
        row = c.fetchone()
        if row:
            zone_data = {'id': row[0], 'coords': row[1], 'comment': row[2], 'assigned_to': row[3]}
            save_to_trash('zone', zone_data)
            print(f"🗑️ Удалена зона ID {row[0]}, сохранена в trash")
        c.execute("DELETE FROM zones WHERE id = %s", (data.get('id'),))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/update_zone_location', methods=['POST'])
def update_zone_location():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("UPDATE zones SET coords = %s WHERE id = %s", (json.dumps(data.get('coords')), data.get('id')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/get_routes')
def get_routes():
    try:
        user = request.args.get('user', '')
        conn = get_db_connection()
        if not conn: return jsonify([]), 200
        c = conn.cursor()
        if user:
            c.execute("SELECT id, coords, comment, temporary, assigned_to FROM routes WHERE temporary = FALSE AND (assigned_to = %s OR assigned_to IS NULL)", (user,))
        else:
            c.execute("SELECT id, coords, comment, temporary, assigned_to FROM routes WHERE temporary = FALSE")
        routes = []
        for r in c.fetchall():
            try:
                coords_list = json.loads(r[1]) if r[1] else []
                routes.append({"id": r[0], "coords": coords_list, "comment": r[2] or "", "assigned_to": r[4] if len(r) > 4 else None})
            except:
                continue
        conn.close()
        print(f"✅ /get_routes: возвращено {len(routes)} маршрутов")
        return jsonify(routes)
    except Exception as e:
        print(f"❌ /get_routes: {e}")
        traceback.print_exc()
        return jsonify([]), 200

@app.route('/get_temp_routes')
def get_temp_routes():
    try:
        conn = get_db_connection()
        if not conn: return jsonify([]), 200
        c = conn.cursor()
        c.execute("SELECT id, coords, comment FROM routes WHERE temporary = TRUE")
        routes = []
        for r in c.fetchall():
            try:
                coords_list = json.loads(r[1]) if r[1] else []
                routes.append({"id": r[0], "coords": coords_list, "comment": r[2] or ""})
            except:
                continue
        conn.close()
        print(f"✅ /get_temp_routes: возвращено {len(routes)} временных маршрутов")
        return jsonify(routes)
    except Exception as e:
        print(f"❌ /get_temp_routes: {e}")
        traceback.print_exc()
        return jsonify([]), 200

@app.route('/clear_temp_routes', methods=['POST'])
def clear_temp_routes():
    try:
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("DELETE FROM routes WHERE temporary = TRUE")
        count = c.rowcount
        conn.commit(); conn.close()
        print(f"️ Очищено {count} временных маршрутов")
        return jsonify({"success": True, "count": count})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/add_route', methods=['POST'])
def add_route():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        temp_value = data.get('temporary', False)
        if isinstance(temp_value, str):
            temp_value = temp_value.lower() == 'true'
        else:
            temp_value = bool(temp_value)
        c.execute("INSERT INTO routes (coords, comment, temporary) VALUES (%s, %s, %s)", 
                 (json.dumps(data.get('coords')), data.get('comment', ''), temp_value))
        conn.commit(); conn.close()
        print(f"✅ Маршрут сохранён, temporary={temp_value}")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/delete_route', methods=['POST'])
def delete_route():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("DELETE FROM routes WHERE id = %s", (data.get('id'),))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/update_route', methods=['POST'])
def update_route():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("UPDATE routes SET coords = %s WHERE id = %s", (json.dumps(data.get('coords')), data.get('id')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/assign_task', methods=['POST'])
def assign_task():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        task_type = data.get('type')
        task_id = data.get('id')
        user = data.get('user')
        if task_type == 'point':
            c.execute("UPDATE points SET assigned_to = %s WHERE id = %s", (user, task_id))
        elif task_type == 'zone':
            c.execute("UPDATE zones SET assigned_to = %s WHERE id = %s", (user, task_id))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/assign_route', methods=['POST'])
def assign_route():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("UPDATE routes SET assigned_to = %s WHERE id = %s", (data.get('user'), data.get('id')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/clear_assignments', methods=['POST'])
def clear_assignments():
    try:
        data = request.json
        user = data.get('user')
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("UPDATE points SET assigned_to = NULL WHERE assigned_to = %s", (user,))
        c.execute("UPDATE zones SET assigned_to = NULL WHERE assigned_to = %s", (user,))
        c.execute("UPDATE routes SET assigned_to = NULL WHERE assigned_to = %s", (user,))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/get_departures')
def get_departures():
    try:
        conn = get_db_connection()
        if not conn: return jsonify([]), 200
        c = conn.cursor()
        c.execute("SELECT point_id, point_title, person_name FROM departures")
        departures = [{"point_id": r[0], "point_title": r[1], "person_name": r[2]} for r in c.fetchall()]
        conn.close()
        return jsonify(departures)
    except Exception as e:
        print(f"❌ /get_departures: {e}")
        return jsonify([]), 200

@app.route('/add_departure', methods=['POST'])
def add_departure():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("""INSERT INTO departures (point_id, point_title, person_name) VALUES (%s, %s, %s)
                     ON CONFLICT (point_id) DO UPDATE SET point_title = EXCLUDED.point_title, person_name = EXCLUDED.person_name""", 
                  (data.get('point_id'), data.get('point_title'), data.get('person_name')))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

@app.route('/remove_departure', methods=['POST'])
def remove_departure():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("DELETE FROM departures WHERE point_id = %s", (data.get('point_id'),))
        conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500

def stop_bot_safely():
    global bot_running
    print(" Останавливаю бота...")
    bot_running = False
    try:
        bot.stop_polling()
        print("✅ Polling остановлен")
    except Exception as e:
        print(f"⚠️ Ошибка при остановке polling: {e}")

def run_bot():
    global bot_running
    if bot_running:
        print("️ Бот уже запущен, пропускаем")
        return
    bot_running = True
    print("🤖 Запускаю бота...")
    try:
        bot.remove_webhook()
        print("✅ Webhook очищен")
    except Exception as e:
        print(f"️ Не удалось очистить webhook: {e}")
    print("⏳ Ожидаю 15 секунд...")
    time.sleep(15)
    while bot_running:
        try:
            print("🔄 Запускаю polling...")
            bot.polling(none_stop=True, timeout=120, long_polling_timeout=120, allowed_updates=["message"])
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 409:
                print("️ Конфликт 409, жду 90 секунд...")
                try: bot.stop_polling()
                except: pass
                time.sleep(90)
            else:
                print(f"❌ Ошибка Telegram API {e.error_code}: {e}")
                time.sleep(30)
        except Exception as e:
            print(f"❌ Ошибка polling: {e}")
            traceback.print_exc()
            try: bot.stop_polling()
            except: pass
            time.sleep(30)

def handle_signal(signum, frame):
    print(f"🛑 Получен сигнал {signum}, завершаю работу...")
    stop_bot_safely()
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

if __name__ == '__main__':
    print("🚀 Запуск сервера...")
    print(f"PID процесса: {os.getpid()}")
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    app.run(host='0.0.0.0', port=8080, threaded=True)
