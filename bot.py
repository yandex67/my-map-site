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

active_monitors = {}
active_call_monitors = {}
pending_confirmations = {}

DEFAULT_GROUP_ID = -1003920057901
DEFAULT_FZ_THREAD_ID = 79227
DEFAULT_CALL_THREAD_ID = 22277
OWNER_USER_ID = 7929131842
TGK_THREAD_ID = 8694

monitored_group_id = DEFAULT_GROUP_ID
monitored_thread_id = DEFAULT_FZ_THREAD_ID
call_thread_id = DEFAULT_CALL_THREAD_ID
monitored_user_id = None
startup_notification_sent = False

def log_to_owner(text, parse_mode=None):
    try:
        bot.send_message(OWNER_USER_ID, text, parse_mode=parse_mode)
    except Exception as e:
        print(f"❌ Ошибка отправки лога в ЛС: {e}")

@app.before_request
def handle_options():
    if request.method == 'OPTIONS': return '', 200

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
        print(f"❌ Ошибка подключения к БД: {e}")
        return None

def column_exists(c, table, column):
    try:
        c.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name='{table}' AND column_name='{column}'")
        return c.fetchone() is not None
    except:
        return False

def save_monitors_to_db():
    try:
        conn = get_db_connection()
        if not conn: return
        c = conn.cursor()
        c.execute("DELETE FROM active_monitors")
        c.execute("DELETE FROM active_call_monitors")
        for key, monitor in active_monitors.items():
            c.execute("""INSERT INTO active_monitors (key, chat_id, thread_id, last_three, user_id, start_time, active) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                     (key, monitor['chat_id'], monitor.get('thread_id'), monitor['last_three'], monitor['user_id'], monitor['start_time'].isoformat(), monitor['active']))
        for key, monitor in active_call_monitors.items():
            c.execute("""INSERT INTO active_call_monitors (key, chat_id, thread_id, user_id, start_time, active) VALUES (%s, %s, %s, %s, %s, %s)""",
                     (key, monitor['chat_id'], monitor.get('thread_id'), monitor['user_id'], monitor['start_time'].isoformat(), monitor['active']))
        
        all_monitors = list(active_monitors.values()) + list(active_call_monitors.values())
        if all_monitors:
            last_user_id = all_monitors[-1].get('user_id')
            if last_user_id:
                c.execute("""INSERT INTO bot_settings (key, value) VALUES ('monitored_user_id', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (str(last_user_id),))
        
        c.execute("""INSERT INTO bot_settings (key, value) VALUES ('monitored_group_id', %s), ('monitored_thread_id', %s), ('call_thread_id', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                 (str(monitored_group_id), str(monitored_thread_id), str(call_thread_id)))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"❌ Ошибка сохранения в БД: {e}")

def load_monitors_from_db():
    global monitored_group_id, monitored_thread_id, call_thread_id, startup_notification_sent, monitored_user_id
    try:
        conn = get_db_connection()
        if not conn: return
        c = conn.cursor()
        c.execute("SELECT key, value FROM bot_settings")
        for row in c.fetchall():
            key, value = row
            if value is None: continue
            if key == 'monitored_group_id': monitored_group_id = int(value)
            elif key == 'monitored_thread_id': monitored_thread_id = int(value)
            elif key == 'call_thread_id': call_thread_id = int(value)
            elif key == 'monitored_user_id': 
                try: monitored_user_id = int(value)
                except: pass
                
        c.execute("SELECT key, chat_id, thread_id, last_three, user_id, start_time, active FROM active_monitors")
        for row in c.fetchall():
            key, chat_id, thread_id, last_three, user_id, start_time_str, active = row
            if active: active_monitors[key] = {'chat_id': chat_id, 'thread_id': thread_id, 'last_three': last_three, 'user_id': user_id, 'start_time': datetime.now(), 'active': True}
        
        c.execute("SELECT key, chat_id, thread_id, user_id, start_time, active FROM active_call_monitors")
        for row in c.fetchall():
            key, chat_id, thread_id, user_id, start_time_str, active = row
            if active: active_call_monitors[key] = {'chat_id': chat_id, 'thread_id': thread_id, 'user_id': user_id, 'start_time': datetime.now(), 'active': True}
        
        conn.close()
        
        call_monitor_key = f"{monitored_group_id}_{call_thread_id}_call"
        if call_monitor_key not in active_call_monitors:
            target_user = monitored_user_id or (list(active_monitors.values())[0].get('user_id') if active_monitors else OWNER_USER_ID)
            if target_user:
                active_call_monitors[call_monitor_key] = {'chat_id': monitored_group_id, 'thread_id': call_thread_id, 'user_id': target_user, 'start_time': datetime.now(), 'active': True}
                save_monitors_to_db()

        if not startup_notification_sent:
            msg = f"🤖 Я проснулся!\n\n✅ Мониторинги перезапущены.\n📊 Сейчас активно:\n• ФЗ: {len(active_monitors)}\n• Обзвоны: {len(active_call_monitors)}"
            try: bot.send_message(OWNER_USER_ID, msg)
            except Exception: pass
            startup_notification_sent = True
    except Exception as e:
        print(f"❌ Ошибка загрузки из БД: {e}")

def init_db():
    global db_initialized
    if db_initialized: return
    try:
        conn = get_db_connection()
        if not conn: return
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS points (id SERIAL PRIMARY KEY, title TEXT, address TEXT, lat REAL, lon REAL, status TEXT DEFAULT 'active', comment TEXT, url TEXT, tgk_url TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS parkings (id SERIAL PRIMARY KEY, title TEXT UNIQUE, address TEXT, lat REAL, lon REAL, available_count TEXT DEFAULT '?', dead_count TEXT DEFAULT '?', broken_count TEXT DEFAULT '?', photo_url TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS zones (id SERIAL PRIMARY KEY, coords TEXT, comment TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS routes (id SERIAL PRIMARY KEY, coords TEXT, comment TEXT, temporary BOOLEAN DEFAULT FALSE)''')
        c.execute('''CREATE TABLE IF NOT EXISTS departures (id SERIAL PRIMARY KEY, point_id INTEGER UNIQUE, point_title TEXT, person_name TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS trash (id SERIAL PRIMARY KEY, type TEXT, data JSON, deleted_at TIMESTAMP DEFAULT NOW())''')
        c.execute('''CREATE TABLE IF NOT EXISTS active_monitors (key TEXT PRIMARY KEY, chat_id BIGINT, thread_id BIGINT, last_three TEXT, user_id BIGINT, start_time TIMESTAMP, active BOOLEAN)''')
        c.execute('''CREATE TABLE IF NOT EXISTS active_call_monitors (key TEXT PRIMARY KEY, chat_id BIGINT, thread_id BIGINT, user_id BIGINT, start_time TIMESTAMP, active BOOLEAN)''')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT)''')
        conn.commit()
        
        for col, tbl in [('comment', 'points'), ('url', 'points'), ('assigned_to', 'points'), ('tgk_url', 'points'), ('photo_url', 'parkings'), ('assigned_to', 'zones'), ('temporary', 'routes'), ('assigned_to', 'routes')]:
            if not column_exists(c, tbl, col): 
                if col != 'temporary': c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT")
                else: c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} BOOLEAN DEFAULT FALSE")
                conn.commit()
        
        for key, value in [('hide_empty_labels', 'false'), ('show_parkings', 'true'), ('show_labels', 'true')]:
            c.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (key, value))
        conn.commit(); db_initialized = True
    except Exception as e:
        print(f"❌ Ошибка инициализации БД: {e}")

@app.before_request
def ensure_db_initialized(): init_db()

def save_to_trash(obj_type, data):
    try:
        conn = get_db_connection()
        if not conn: return
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM trash WHERE type = %s", (obj_type,))
        if c.fetchone()[0] >= 5: c.execute("DELETE FROM trash WHERE id IN (SELECT id FROM trash WHERE type = %s ORDER BY deleted_at ASC LIMIT 1)", (obj_type,))
        c.execute("INSERT INTO trash (type, data) VALUES (%s, %s)", (obj_type, json.dumps(data, ensure_ascii=False, default=str)))
        conn.commit(); conn.close()
    except Exception: pass

def restore_from_trash():
    try:
        conn = get_db_connection()
        if not conn: return None, "Нет подключения к БД"
        c = conn.cursor()
        c.execute("SELECT id, type, data FROM trash ORDER BY deleted_at DESC LIMIT 1")
        row = c.fetchone()
        if not row: conn.close(); return None, "Корзина пуста"
        trash_id, obj_type, data = row
        data = json.loads(data) if isinstance(data, str) else data
        if obj_type in ('point', 'info'):
            c.execute("""INSERT INTO points (title, address, lat, lon, status, comment, url, assigned_to, tgk_url) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                     (data.get('title'), data.get('address'), data.get('lat'), data.get('lon'), data.get('status', 'active'), data.get('comment', ''), data.get('url', ''), data.get('assigned_to'), data.get('tgk_url', '')))
        elif obj_type == 'zone':
            c.execute("""INSERT INTO zones (coords, comment, assigned_to) VALUES (%s, %s, %s) RETURNING id""", (data.get('coords'), data.get('comment', ''), data.get('assigned_to')))
        elif obj_type == 'parking':
            c.execute("""INSERT INTO parkings (title, address, lat, lon, available_count, dead_count, broken_count, photo_url) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                     (data.get('title'), data.get('address'), data.get('lat'), data.get('lon'), data.get('available_count', '?'), data.get('dead_count', '?'), data.get('broken_count', '?'), data.get('photo_url')))
        else: conn.close(); return None, f"Неизвестный тип: {obj_type}"
        new_id = c.fetchone()[0]
        c.execute("DELETE FROM trash WHERE id = %s", (trash_id,))
        conn.commit(); conn.close()
        return f"✅ Восстановлено (ID: {new_id})", None
    except Exception as e:
        return None, str(e)

def build_address_queries(address):
    a = re.sub(r'^(г\.?\s*)?Смоленск\s*,?\s*', '', ' '.join(address.split()).strip(), flags=re.IGNORECASE).strip()
    m = re.match(r'^(?:улица|ул\.?|проспект|пр-кт|переулок|пер\.?|бульвар|б-р)?\s*([а-яА-ЯёЁ\w\-]+)\s*,?\s*(\d+[А-Яа-яЁё]?)$', a, re.IGNORECASE)
    if m:
        variants = [f"Смоленск, улица {m.group(1)}, {m.group(2)}", f"г. Смоленск, улица {m.group(1)}, {m.group(2)}", f"Смоленск, {m.group(1)} {m.group(2)}", f"Смоленск, улица {m.group(1)}", f"Смоленск, {m.group(1)}"]
    else:
        variants = [f"Смоленск, {a}", f"г. Смоленск, {a}", f"Смоленск, улица {a}"]
    return list(dict.fromkeys(variants))

def get_coordinates(address):
    for i, query in enumerate(build_address_queries(address)):
        if i > 0: time.sleep(1.1)
        try:
            response = requests.get("https://nominatim.openstreetmap.org/search", params={"q": query, "format": "json", "limit": 1}, headers={"User-Agent": "SmolenskMapBot/1.0"}, timeout=8)
            if response.status_code == 429: time.sleep(3); continue
            if response.status_code == 200:
                data = response.json()
                if data and 'lat' in data[0] and 'lon' in data[0]: return float(data[0]["lat"]), float(data[0]["lon"])
        except Exception: continue
    return None, None

def parse_scooter_command(text):
    m = re.match(r'^(.+?)\s*-\s*(\d{3})(?:\s+(.*))?$', ' '.join(text.split()).strip(), re.IGNORECASE)
    if not m: return None
    location, last_three, rest = m.group(1).strip(), m.group(2), (m.group(3) or "").strip()
    if rest.lower().endswith('p') or rest.lower() == 'удалить': return None
    return location, last_three, rest

def parse_scooter_message_8694(text):
    scooter_match = re.search(r'(?:🛴\s*)?7703(\d{3})\b', text)
    if not scooter_match: return None
    last_three = scooter_match.group(1)
    address = None
    for pattern in [r'Самокат GPS:\s*(.+?)(?:\n|$)', r'Текущий gps:\s*(.+?)(?:\n|$)', r'г\.?\s*Смоленск,\s*((?:ул\.?|улица|пр-кт|проспект|переулок|пер\.?)\s+[^,\n]+(?:,\s*(?:д\.?\s*)?\d+[А-Яа-яЁё]?)?)']:
        match = re.search(pattern, text, re.IGNORECASE)
        if match: address = match.group(1).strip(); break
    if not address and re.search(r'Нет координат в пределах города', text, re.IGNORECASE): address = "54.776443, 32.050324"
    if not address: return None
    
    # Нормализация для 8694
    a = re.sub(r'^г\.?\s*Смоленск\s*,?\s*', '', address.strip(), flags=re.IGNORECASE).strip()
    a = re.sub(r',?\s*(?:д\.?|дом\.?)\s*', ' ', a, flags=re.IGNORECASE).strip()
    a = re.sub(r',\s*', ' ', a).strip()
    a = re.sub(r'\s+', ' ', a).strip()
    
    m = re.match(r'^(.+?)\s+(ул\.?|улица|пр-кт|проспект|переулок|пер\.?|бульвар|б-р)\.?\s+(\d+[А-Яа-яЁё]?)$', a, re.IGNORECASE)
    if m: address = f"{m.group(1).strip()} {m.group(3).strip()}"
    else:
        m = re.match(r'^([а-яА-ЯёЁ\w\-]+(?:\s+[а-яА-ЯёЁ\w\-]+)*)\s+(\d+[А-Яа-яЁё]?)$', a)
        if m: address = f"{m.group(1).strip()} {m.group(2).strip()}"
        
    return (address, last_three, 'sev') if len(address) > 3 else None

def create_scooter_only(address, last_three, status, tgk_url):
    try:
        conn = get_db_connection()
        if not conn: return False, "Нет подключения к БД"
        c = conn.cursor()
        c.execute("SELECT id, tgk_url FROM points WHERE title = %s AND status != 'found'", (last_three,))
        existing = c.fetchone()
        if existing:
            if not existing[1] and tgk_url:
                c.execute("UPDATE points SET tgk_url = %s WHERE id = %s", (tgk_url, existing[0])); conn.commit()
            conn.close(); return True, "Самокат уже существовал"
        c.execute("""INSERT INTO points (title, address, lat, lon, status, comment, url, tgk_url) VALUES (%s, %s, NULL, NULL, %s, '', '', %s)""", (last_three, address, status, tgk_url))
        conn.commit()
        c.execute("SELECT id FROM points WHERE title = %s AND status != 'found'", (last_three,))
        created = c.fetchone()
        conn.close()
        return (True, "Успешно создан") if created else (False, "Запись не найдена в БД")
    except Exception as e: return False, str(e)

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

@bot.message_handler(regexp=r'^удали тред айди$')
def delete_thread_id(message):
    if message.chat.type != 'private': return
    global monitored_thread_id, call_thread_id
    monitored_thread_id, call_thread_id = DEFAULT_FZ_THREAD_ID, DEFAULT_CALL_THREAD_ID
    save_monitors_to_db()
    bot.reply_to(message, f"✅ Thread ID сброшены:\nФЗ: {DEFAULT_FZ_THREAD_ID}\nОбзвоны: {DEFAULT_CALL_THREAD_ID}")

@bot.message_handler(regexp=r'^тред обзвон off$')
def reset_call_thread(message):
    if message.chat.type != 'private': return
    global call_thread_id
    call_thread_id = DEFAULT_CALL_THREAD_ID; save_monitors_to_db()
    bot.reply_to(message, f"✅ Обзвоны сброшены: {DEFAULT_CALL_THREAD_ID}")

@bot.message_handler(regexp=r'^тред фз off$')
def reset_fz_thread(message):
    if message.chat.type != 'private': return
    global monitored_thread_id
    monitored_thread_id = DEFAULT_FZ_THREAD_ID; save_monitors_to_db()
    bot.reply_to(message, f"✅ ФЗ сброшен: {DEFAULT_FZ_THREAD_ID}")

@bot.message_handler(regexp=r'^тред фз (\d+)$')
def set_fz_thread(message):
    if message.chat.type != 'private': return
    global monitored_thread_id
    monitored_thread_id = int(message.matches[0].group(1)); save_monitors_to_db()
    bot.reply_to(message, f"✅ ФЗ тред установлен: {monitored_thread_id}")

@bot.message_handler(regexp=r'^тред обзвон (\d+)$')
def set_call_thread(message):
    if message.chat.type != 'private': return
    global call_thread_id
    call_thread_id = int(message.matches[0].group(1)); save_monitors_to_db()
    bot.reply_to(message, f"✅ Обзвоны тред установлен: {call_thread_id}")

@bot.message_handler(regexp=r'^тредайдикакой\?$')
def show_thread_id(message):
    user_id = message.from_user.id
    if message.chat.type == 'private':
        bot.send_message(user_id, f"📊 Текущие настройки:\n💬 Группа ID: {monitored_group_id}\n📂 ФЗ Thread ID: {monitored_thread_id}\n📞 Обзвоны Thread ID: {call_thread_id}\n🔗 TGK авто Thread ID: {TGK_THREAD_ID}")
    elif message.chat.type in ['group', 'supergroup']:
        thread_id = getattr(message, 'message_thread_id', None)
        if thread_id: bot.send_message(user_id, f"📂 ID этого треда: {thread_id}\n\nЧтобы задать для ФЗ: тред фз {thread_id}\nДля обзвонов: тред обзвон {thread_id}")
        else: bot.send_message(user_id, "⚠️ Это общий чат. Напиши команду внутри треда.")

@bot.message_handler(commands=['мой_айди', 'myid'])
def show_my_id(message):
    if message.chat.type != 'private': return
    bot.reply_to(message, f"🆔 Твой user_id: `{message.from_user.id}`\nOWNER_USER_ID: `{OWNER_USER_ID}`\nmonitored_user_id: `{monitored_user_id}`", parse_mode='Markdown')

@bot.message_handler(commands=['мониторь'])
def start_monitor(message):
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit() or len(parts[1]) != 3:
        bot.reply_to(message, "Использование: /мониторь <последние_3_цифры>"); return
    last_three = parts[1]
    monitor_key = f"{monitored_group_id}_{monitored_thread_id}_{last_three}"
    if monitor_key in active_monitors and active_monitors[monitor_key]['active']:
        bot.reply_to(message, f"⚠️ Мониторинг 7703{last_three} уже активен!"); return
    active_monitors[monitor_key] = {'chat_id': monitored_group_id, 'thread_id': monitored_thread_id, 'last_three': last_three, 'user_id': message.from_user.id, 'start_time': datetime.now(), 'active': True}
    save_monitors_to_db()
    bot.reply_to(message, f"✅ Мониторинг 7703{last_three} запущен!")

@bot.message_handler(commands=['стоп'])
def stop_monitor(message):
    parts = message.text.strip().split()
    if len(parts) < 2: return
    monitor_key = f"{monitored_group_id}_{monitored_thread_id}_{parts[1]}"
    if monitor_key in active_monitors:
        del active_monitors[monitor_key]; save_monitors_to_db()
        bot.reply_to(message, f"❌ Мониторинг 7703{parts[1]} остановлен")

@bot.message_handler(commands=['мониторы'])
def list_monitors(message):
    user_monitors = {k: v for k, v in active_monitors.items() if v['user_id'] == message.from_user.id and v['active']}
    call_monitor_active = f"{monitored_group_id}_{call_thread_id}_call" in active_call_monitors
    text = "📊 Все мониторинги:\n\n🔍 ФЗ:\n" + ("".join([f"  • 7703{m['last_three']}\n" for m in user_monitors.values()]) or "  (нет)\n")
    text += f"\n📞 Обзвоны:\n  ✅ {'АКТИВЕН' if call_monitor_active else 'НЕ АКТИВЕН'}\n"
    text += f"\n🔗 Авто-создание самокатов + КАРТОЧКА В ТГ:\n  ✅ Всегда активен (топик {TGK_THREAD_ID})\n"
    bot.reply_to(message, text)

@bot.message_handler(commands=['off'])
def off_monitors(message):
    text = message.text.strip().lower()
    if 'все' in text or 'все мониторы' in text:
        keys = [k for k, v in active_monitors.items() if v['user_id'] == message.from_user.id and v['active']]
        for k in keys: del active_monitors[k]
        save_monitors_to_db(); bot.reply_to(message, f"🗑️ Удалено: {len(keys)}")
    elif 'last' in text:
        user_m = {k: v for k, v in active_monitors.items() if v['user_id'] == message.from_user.id and v['active']}
        if user_m:
            last_key = max(user_m.keys(), key=lambda k: user_m[k]['start_time'])
            del active_monitors[last_key]; save_monitors_to_db()
            bot.reply_to(message, "🗑️ Последний удалён")

@bot.message_handler(commands=['обзвоны'])
def start_call_monitor(message):
    call_monitor_key = f"{monitored_group_id}_{call_thread_id}_call"
    if call_monitor_key not in active_call_monitors:
        active_call_monitors[call_monitor_key] = {'chat_id': monitored_group_id, 'thread_id': call_thread_id, 'user_id': message.from_user.id, 'start_time': datetime.now(), 'active': True}
        save_monitors_to_db()
    bot.reply_to(message, "✅ Мониторинг обзвонов активен!")

@bot.message_handler(commands=['стоп_обзвоны'])
def stop_call_monitor(message):
    monitor_key = f"{monitored_group_id}_{call_thread_id}_call"
    if monitor_key in active_call_monitors:
        del active_call_monitors[monitor_key]; save_monitors_to_db()
        bot.reply_to(message, "❌ Мониторинг обзвонов остановлен")

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if message.text is None: return
    text = message.text.strip()
    text_lower = text.lower()
    user_id = message.from_user.id

    if message.chat.type == 'private' and monitored_user_id != user_id:
        monitored_user_id = user_id
        try:
            conn = get_db_connection()
            if conn:
                c = conn.cursor()
                c.execute("""INSERT INTO bot_settings (key, value) VALUES ('monitored_user_id', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (str(user_id),))
                conn.commit(); conn.close()
        except Exception: pass

    if user_id in pending_confirmations:
        if text_lower == 'да':
            action = pending_confirmations.pop(user_id)
            if action == 'delete_all_scooters':
                try:
                    conn = get_db_connection()
                    if conn:
                        c = conn.cursor(); c.execute("DELETE FROM points"); count = c.rowcount; conn.commit(); conn.close()
                        bot.reply_to(message, f"🗑️ Удалено всех самокатов: {count}")
                except Exception as e: bot.reply_to(message, f"❌ Ошибка: {e}")
        else:
            pending_confirmations.pop(user_id); bot.reply_to(message, "❌ Отменено.")
        return

    if message.chat.type in ['group', 'supergroup']:
        msg_thread_id = getattr(message, 'message_thread_id', None)
        message_time = datetime.fromtimestamp(message.date)
        
        for key, monitor in list(active_monitors.items()):
            if monitor['chat_id'] == monitored_group_id and monitor['active']:
                if monitor.get('thread_id') is not None and msg_thread_id != monitor['thread_id']: continue
                if message_time < monitor['start_time']: continue
                if f"7703{monitor['last_three']}" in message.text:
                    try:
                        raw_chat_id = str(monitored_group_id)
                        chat_id_for_link = raw_chat_id[4:] if raw_chat_id.startswith('-100') else raw_chat_id.lstrip('-')
                        bot.send_message(monitor['user_id'], f"Произошло новое ФЗ {monitor['last_three']} !\n\n🔗 https://t.me/c/{chat_id_for_link}/{message.message_id}", disable_web_page_preview=True)
                        del active_monitors[key]; save_monitors_to_db()
                    except Exception: pass

        for key, monitor in list(active_call_monitors.items()):
            if monitor['chat_id'] == monitored_group_id and monitor['active']:
                if monitor.get('thread_id') is not None and msg_thread_id != monitor['thread_id']: continue
                if 'Добавлен результат обзвона' in message.text:
                    try: bot.forward_message(monitor['user_id'], monitored_group_id, message.message_id)
                    except Exception: pass

        if msg_thread_id == call_thread_id and ('из поис' in text_lower) and ('удален' in text_lower or 'удалён' in text_lower):
            match = re.search(r'7703(\d{3})', message.text)
            if match:
                last_three = match.group(1)
                try:
                    conn = get_db_connection()
                    if conn:
                        c = conn.cursor()
                        c.execute("DELETE FROM points WHERE title = %s", (last_three,))
                        if c.rowcount > 0:
                            bot.send_message(OWNER_USER_ID, f"🗑️ {last_three} удалён с карты!")
                        conn.commit(); conn.close()
                except Exception: pass

        if msg_thread_id == call_thread_id and 'удален из без аренды' in text_lower:
            match = re.search(r'7703(\d{3})', message.text)
            if match:
                bot.send_message(OWNER_USER_ID, f"{match.group(1)} кто-то арендовал спустя 48 часов или более.")

        if msg_thread_id == TGK_THREAD_ID:
            log_to_owner(f"📥 Получено сообщение в топике {TGK_THREAD_ID}:\n'{message.text}'")
            parsed = parse_scooter_message_8694(message.text)
            if parsed:
                address, last_three, status = parsed
                log_to_owner(f"👁️ Я увидел новый Поиск самокат номер {last_three}")
                raw_chat_id = str(monitored_group_id)
                chat_id_for_link = raw_chat_id[4:] if raw_chat_id.startswith('-100') else raw_chat_id.lstrip('-')
                message_link = f"https://t.me/c/{chat_id_for_link}/{message.message_id}"
                log_to_owner(f"⚙️ Создаю его на карте и добавляю ссылку на пост для {last_three}")
                success, reason = create_scooter_only(address, last_three, status, message_link)
                if success:
                    command_text = f"{address} - {last_three} {status} {message_link}"
                    log_to_owner(f"✅ Успешно создано! Отправляю себе команду:\n`{command_text}`", parse_mode='Markdown')
                    bot.send_message(OWNER_USER_ID, command_text)
                else:
                    log_to_owner(f"❌ Не удалось создать самокат {last_three}. По причине: {reason}")
            else:
                log_to_owner(f"⚠️ Сообщение в топике {TGK_THREAD_ID} не распознано как самокат.")
        return

    # ==============================================================================
    # ✅ ЛИЧНЫЕ СООБЩЕНИЯ: СОЗДАНИЕ САМОКАТА (С ПОДРОБНЫМИ ЛОГАМИ)
    # ==============================================================================
    scooter_cmd = parse_scooter_command(text)
    if scooter_cmd:
        print(f"🔍 РАСПОЗНАНА КОМАНДА СОЗДАНИЯ: {scooter_cmd}")
        location_part, last_three, rest = scooter_cmd
        tgk_url, comment = "", ""
        
        url_match = re.search(r'(https?://\S+)', rest, re.IGNORECASE)
        if url_match:
            tgk_url = url_match.group(1)
            rest = rest.replace(tgk_url, "").strip()
        
        status = 'active'
        if re.search(r'\bsev\b', rest, re.IGNORECASE):
            status = 'sev'
            rest = re.sub(r'\bsev\b', '', rest, flags=re.IGNORECASE).strip()
        elif re.search(r'\bred\b', rest, re.IGNORECASE):
            status = 'red'
            rest = re.sub(r'\bred\b', '', rest, flags=re.IGNORECASE).strip()
        
        comment = rest.strip()
        coord_match = re.match(r'^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$', location_part)
        
        if coord_match:
            lat, lon = float(coord_match.group(1)), float(coord_match.group(2))
            print(f"📍 Использованы прямые координаты: {lat}, {lon}")
        else:
            print(f"🔍 Ищем координаты для адреса: '{location_part}'")
            lat, lon = get_coordinates(location_part)
            
        if lat is None or lon is None:
            print(f"❌ Не удалось найти координаты для: {location_part}")
            bot.reply_to(message, f"⚠️ Не нашёл адрес '{location_part}'.\nПопробуй написать полнее, например: улица Ленина, 11 - {last_three}")
            return
            
        print(f"✅ Координаты найдены: {lat}, {lon}. Записываем в БД...")
        try:
            conn = get_db_connection()
            if conn:
                c = conn.cursor()
                c.execute("""INSERT INTO points (title, address, lat, lon, status, comment, url, tgk_url) 
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                         (last_three, location_part, lat, lon, status, comment, '', tgk_url))
                conn.commit()
                conn.close()
                print(f"✅ Самокат {last_three} успешно создан в БД!")
                
                answer = f"✅ Самокат {last_three} добавлен!\n📍 {location_part}\n🎨 {status}"
                if comment: answer += f"\n💬 {comment}"
                if tgk_url: answer += "\n🔗 КАРТОЧКА В ТГ привязана"
                bot.reply_to(message, answer)
        except Exception as e:
            print(f"❌ ОШИБКА ЗАПИСИ В БД: {e}")
            traceback.print_exc()
            bot.reply_to(message, f"❌ Ошибка при создании: {e}")
        return

    if text_lower == 'z':
        res, err = restore_from_trash(); bot.reply_to(message, f"❌ {err}" if err else res); return

    if text_lower in ('trash', 'корзина'):
        try:
            conn = get_db_connection(); c = conn.cursor()
            c.execute("SELECT type, data FROM trash ORDER BY deleted_at DESC"); items = c.fetchall(); conn.close()
            if not items: bot.reply_to(message, "🗑️ Корзина пуста")
            else:
                msg = "📦 Корзина:\n" + "".join([f"{i}. {obj_type}: {json.loads(data).get('title', '')}\n" for i, (obj_type, data) in enumerate(items, 1)])
                bot.reply_to(message, msg + "\nНапиши Z чтобы восстановить")
        except Exception: pass

    if text_lower.startswith('off ') and len(text_lower.split()) == 2:
        last_three = text_lower.split()[1]
        if last_three.isdigit() and len(last_three) == 3:
            try:
                conn = get_db_connection()
                if conn:
                    c = conn.cursor(); c.execute("DELETE FROM points WHERE title = %s", (last_three,))
                    count = c.rowcount; conn.commit(); conn.close()
                    bot.reply_to(message, f"🗑️ Самокат {last_three} удалён." if count > 0 else f"❌ Самокат {last_three} не найден.")
            except Exception: pass
        return

    if text_lower == 'off last sam':
        try:
            conn = get_db_connection()
            if conn:
                c = conn.cursor(); c.execute("DELETE FROM points WHERE id = (SELECT MAX(id) FROM points)")
                count = c.rowcount; conn.commit(); conn.close()
                bot.reply_to(message, "🗑️ Последний самокат удалён." if count > 0 else "❌ Самокатов нет.")
        except Exception: pass
        return

    if text_lower == 'off all sam':
        pending_confirmations[user_id] = 'delete_all_scooters'
        bot.reply_to(message, "⚠️ Точно удалить все самокаты? Напиши 'Да' для подтверждения.")
        return

    if text_lower == 'off все маршруты':
        conn = get_db_connection(); c = conn.cursor(); c.execute("DELETE FROM routes"); count = c.rowcount; conn.commit(); conn.close()
        bot.reply_to(message, f"🗑️ Удалено маршрутов: {count}"); return
    
    if text_lower == 'off last маршрут':
        conn = get_db_connection(); c = conn.cursor(); c.execute("DELETE FROM routes WHERE id = (SELECT MAX(id) FROM routes)")
        conn.commit(); conn.close()
        bot.reply_to(message, "🗑️ Последний маршрут удалён." if c.rowcount > 0 else "❌ Маршрутов нет."); return

    if text_lower == 'off лишнее':
        conn = get_db_connection(); c = conn.cursor(); c.execute("UPDATE settings SET value = 'true' WHERE key = 'hide_empty_labels'"); conn.commit(); conn.close()
        bot.reply_to(message, "✅ Лишние плашки скрыты."); return
    if text_lower == 'вкл всё':
        conn = get_db_connection(); c = conn.cursor(); c.execute("UPDATE settings SET value = 'false' WHERE key = 'hide_empty_labels'"); conn.commit(); conn.close()
        bot.reply_to(message, "✅ Все плашки видны."); return
    if text_lower == 'off видимость парковок':
        conn = get_db_connection(); c = conn.cursor(); c.execute("UPDATE settings SET value = 'false' WHERE key = 'show_parkings'"); conn.commit(); conn.close()
        bot.reply_to(message, "✅ Парковки скрыты."); return
    if text_lower == 'вкл видимость парковок':
        conn = get_db_connection(); c = conn.cursor(); c.execute("UPDATE settings SET value = 'true' WHERE key = 'show_parkings'"); conn.commit(); conn.close()
        bot.reply_to(message, "✅ Парковки видны."); return
    if text_lower == 'off плашки':
        conn = get_db_connection(); c = conn.cursor(); c.execute("UPDATE settings SET value = 'false' WHERE key = 'show_labels'"); conn.commit(); conn.close()
        bot.reply_to(message, "✅ Плашки скрыты."); return
    if text_lower == 'вкл плашки':
        conn = get_db_connection(); c = conn.cursor(); c.execute("UPDATE settings SET value = 'true' WHERE key = 'show_labels'"); conn.commit(); conn.close()
        bot.reply_to(message, "✅ Плашки видны."); return
    if text_lower == 'сброс парковок':
        try:
            conn = get_db_connection(); c = conn.cursor(); c.execute("UPDATE parkings SET available_count = '?', dead_count = '?', broken_count = '?'")
            count = c.rowcount; conn.commit(); conn.close()
            bot.reply_to(message, f"✅ Статистика сброшена ({count} шт.)")
        except Exception: pass

    if text_lower.endswith(' off ссылку'):
        name = text[:-11].strip()
        conn = get_db_connection(); c = conn.cursor(); c.execute("UPDATE parkings SET photo_url = NULL WHERE title = %s", (name,)); conn.commit(); conn.close()
        bot.reply_to(message, "🗑️ Ссылка удалена."); return

    if ' http' in text or text.startswith('http'):
        parts = text.split(' ', 1)
        if len(parts) == 2 and parts[1].startswith('http'):
            name = parts[0].strip()
            if not re.match(r'^\d{3}$', name):
                conn = get_db_connection(); c = conn.cursor(); c.execute("UPDATE parkings SET photo_url = %s WHERE title = %s", (parts[1].strip(), name))
                conn.commit(); conn.close()
                bot.reply_to(message, f"✅ Ссылка на фото парковки '{name}' сохранена.")
                return

    if text_lower.endswith(' - p'):
        clean = text[:-4].strip()
        if '-' in clean:
            loc, title = clean.rsplit('-', 1); loc, title = loc.strip(), title.strip()
            cm = re.match(r'^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$', loc)
            lat, lon = (float(cm.group(1)), float(cm.group(2))) if cm else get_coordinates(loc)
            if lat and lon:
                conn = get_db_connection(); c = conn.cursor(); c.execute("INSERT INTO parkings (title, address, lat, lon) VALUES (%s, %s, %s, %s)", (title, loc, lat, lon))
                conn.commit(); conn.close()
                bot.reply_to(message, f"✅ Парковка '{title}' добавлена!")
            else: bot.reply_to(message, f"❌ Не найдено: '{loc}'")
        return

    if ' - ' in text:
        parts = text.split(' - ', 1); title, action = parts[0].strip(), parts[1].strip()
        if action.lower() == 'удалить':
            conn = get_db_connection(); c = conn.cursor()
            c.execute("SELECT id, title, address, lat, lon, available_count, dead_count, broken_count, photo_url FROM parkings WHERE title = %s", (title,))
            row = c.fetchone()
            if row: save_to_trash('parking', {'id': row[0], 'title': row[1], 'address': row[2], 'lat': row[3], 'lon': row[4], 'available_count': row[5], 'dead_count': row[6], 'broken_count': row[7], 'photo_url': row[8]})
            c.execute("DELETE FROM parkings WHERE title = %s", (title,))
            conn.commit(); conn.close()
            bot.reply_to(message, f"🗑️ Парковка '{title}' удалена." if c.rowcount > 0 else f"❌ Парковка '{title}' не найдена.")
            return
        params = re.findall(r'([СсДдПп])(\d+|\?)', action)
        if params:
            avail = dead = broken = None
            for pt, pv in params:
                if pt.lower() == 'с': dead = pv
                elif pt.lower() == 'д': avail = pv
                elif pt.lower() == 'п': broken = pv
            conn = get_db_connection(); c = conn.cursor()
            c.execute("SELECT available_count, dead_count, broken_count FROM parkings WHERE title = %s", (title,))
            row = c.fetchone()
            if row:
                c.execute("UPDATE parkings SET available_count = %s, dead_count = %s, broken_count = %s WHERE title = %s", (avail or row[0], dead or row[1], broken or row[2], title))
                conn.commit(); conn.close()
                bot.reply_to(message, f"✅ '{title}' обновлена.")
            return

    if text_lower.endswith(' - доп инфа'):
        clean = text[:-10].strip()
        loc = clean.split(' - ')[0].strip() if ' - ' in clean else clean
        comment = clean.split(' - ')[1].strip() if ' - ' in clean else ""
        cm = re.match(r'^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$', loc)
        lat, lon = (float(cm.group(1)), float(cm.group(2))) if cm else get_coordinates(loc)
        if lat and lon:
            conn = get_db_connection(); c = conn.cursor()
            c.execute("INSERT INTO points (title, address, lat, lon, status, comment, url, tgk_url) VALUES (%s, %s, %s, %s, 'info', %s, '', '')", ("ДОП ИНФА", loc, lat, lon, comment))
            conn.commit(); conn.close()
            bot.reply_to(message, "✅ Доп инфа добавлена!")
        return

    if text_lower == 'все p off':
        conn = get_db_connection(); c = conn.cursor(); c.execute("DELETE FROM parkings"); count = c.rowcount; conn.commit(); conn.close()
        bot.reply_to(message, f"🗑️ Удалено парковок: {count}"); return
    if text_lower == 'все с off':
        conn = get_db_connection(); c = conn.cursor(); c.execute("DELETE FROM points"); count = c.rowcount; c.execute("DELETE FROM departures"); conn.commit(); conn.close()
        bot.reply_to(message, f"🗑️ Удалено точек: {count}"); return
    if text_lower == 'last p off':
        conn = get_db_connection(); c = conn.cursor(); c.execute("DELETE FROM parkings WHERE id = (SELECT MAX(id) FROM parkings)")
        conn.commit(); conn.close()
        bot.reply_to(message, "🗑️ Последняя парковка удалена." if c.rowcount > 0 else "❌ Парковок нет."); return
    if text_lower in ('last с off', 'last s off'):
        conn = get_db_connection(); c = conn.cursor(); c.execute("DELETE FROM departures WHERE point_id = (SELECT MAX(id) FROM points)"); c.execute("DELETE FROM points WHERE id = (SELECT MAX(id) FROM points)")
        conn.commit(); conn.close()
        bot.reply_to(message, "🗑️ Последний самокат удалён." if c.rowcount > 0 else "❌ Самокатов нет."); return

# --- API ЭНДПОИНТЫ ---
@app.route('/get_settings')
def get_settings():
    try:
        conn = get_db_connection(); c = conn.cursor(); c.execute("SELECT key, value FROM settings")
        settings = {row[0]: row[1] == 'true' for row in c.fetchall()}; conn.close()
        return jsonify(settings)
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/update_setting', methods=['POST'])
def update_setting():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE settings SET value = %s WHERE key = %s", (str(data.get('value')).lower(), data.get('key')))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/get_points')
def get_points():
    try:
        user = request.args.get('user', ''); conn = get_db_connection()
        if not conn: return jsonify([]), 200
        c = conn.cursor()
        if user: c.execute("SELECT id, title, address, lat, lon, status, comment, url, assigned_to, tgk_url FROM points WHERE status != 'found' AND assigned_to = %s", (user,))
        else: c.execute("SELECT id, title, address, lat, lon, status, comment, url, assigned_to, tgk_url FROM points WHERE status != 'found'")
        points = [{"id": r[0], "title": r[1], "address": r[2], "lat": float(r[3]) if r[3] else 0, "lon": float(r[4]) if r[4] else 0, "status": r[5], "comment": r[6] or "", "url": r[7] or "", "assigned_to": r[8], "tgk_url": r[9] or ""} for r in c.fetchall()]
        conn.close(); return jsonify(points)
    except Exception: return jsonify([]), 200

@app.route('/create_scooter', methods=['POST'])
def create_scooter():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor()
        c.execute("INSERT INTO points (title, address, lat, lon, status, comment, url, tgk_url) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", 
                 (data.get('number', ''), data.get('address', ''), data.get('lat'), data.get('lon'), data.get('status', 'active'), data.get('comment', ''), '', data.get('tgk_url', '')))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/create_parking', methods=['POST'])
def create_parking():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor()
        c.execute("INSERT INTO parkings (title, address, lat, lon) VALUES (%s, %s, %s, %s)", (data.get('title', ''), data.get('address', ''), data.get('lat'), data.get('lon')))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/update_status', methods=['POST'])
def update_status():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor()
        if data.get('status') == 'found':
            c.execute("SELECT id, title, address, lat, lon, status, comment, url, assigned_to, tgk_url FROM points WHERE id = %s", (data.get('id'),))
            row = c.fetchone()
            if row:
                point_data = {'id': row[0], 'title': row[1], 'address': row[2], 'lat': row[3], 'lon': row[4], 'status': row[5], 'comment': row[6], 'url': row[7], 'assigned_to': row[8], 'tgk_url': row[9]}
                save_to_trash('info' if row[5] == 'info' else 'point', point_data)
                if row[5] != 'info':
                    try: bot.send_message(OWNER_USER_ID, f"🎯 Кто-то нашёл самокат {row[1]}!")
                    except Exception: pass
            c.execute("DELETE FROM points WHERE id = %s", (data.get('id'),))
        else:
            c.execute("UPDATE points SET status = %s WHERE id = %s", (data.get('status'), data.get('id')))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/update_comment', methods=['POST'])
def update_comment():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE points SET comment = %s WHERE id = %s", (data.get('comment', ''), data.get('point_id')))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/update_point_location', methods=['POST'])
def update_point_location():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE points SET lat = %s, lon = %s, address = %s WHERE id = %s", (data.get('lat'), data.get('lon'), data.get('address', ''), data.get('id')))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/update_parking_location', methods=['POST'])
def update_parking_location():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE parkings SET lat = %s, lon = %s, address = %s WHERE id = %s", (data.get('lat'), data.get('lon'), data.get('address', ''), data.get('id')))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/get_parkings')
def get_parkings():
    try:
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT id, title, address, lat, lon, available_count, dead_count, broken_count, photo_url FROM parkings")
        parkings = [{"id": r[0], "title": r[1] or "?", "address": r[2] or "?", "lat": float(r[3]), "lon": float(r[4]), "available": r[5] or '?', "dead": r[6] or '?', "broken": r[7] or '?', "photo_url": r[8] or ""} for r in c.fetchall() if r[3] and r[4]]
        conn.close(); return jsonify(parkings)
    except Exception: return jsonify([]), 200

@app.route('/update_parking', methods=['POST'])
def update_parking():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE parkings SET available_count = %s, dead_count = %s, broken_count = %s WHERE id = %s", (data.get('available', '?'), data.get('dead', '?'), data.get('broken', '?'), data.get('id')))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/delete_parking', methods=['POST'])
def delete_parking():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT id, title, address, lat, lon, available_count, dead_count, broken_count, photo_url FROM parkings WHERE id = %s", (data.get('id'),))
        row = c.fetchone()
        if row: save_to_trash('parking', {'id': row[0], 'title': row[1], 'address': row[2], 'lat': row[3], 'lon': row[4], 'available_count': row[5], 'dead_count': row[6], 'broken_count': row[7], 'photo_url': row[8]})
        c.execute("DELETE FROM parkings WHERE id = %s", (data.get('id'),))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/get_zones')
def get_zones():
    try:
        user = request.args.get('user', ''); conn = get_db_connection()
        if not conn: return jsonify([]), 200
        c = conn.cursor()
        if user: c.execute("SELECT id, coords, comment, assigned_to FROM zones WHERE assigned_to = %s", (user,))
        else: c.execute("SELECT id, coords, comment, assigned_to FROM zones")
        zones = [{"id": r[0], "coords": json.loads(r[1]) if r[1] else [], "comment": r[2] or "", "assigned_to": r[3]} for r in c.fetchall()]
        conn.close(); return jsonify(zones)
    except Exception: return jsonify([]), 200

@app.route('/add_zone', methods=['POST'])
def add_zone():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor()
        c.execute("INSERT INTO zones (coords, comment) VALUES (%s, %s)", (json.dumps(data.get('coords')), data.get('comment', '')))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/delete_zone', methods=['POST'])
def delete_zone():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT id, coords, comment, assigned_to FROM zones WHERE id = %s", (data.get('id'),))
        row = c.fetchone()
        if row: save_to_trash('zone', {'id': row[0], 'coords': row[1], 'comment': row[2], 'assigned_to': row[3]})
        c.execute("DELETE FROM zones WHERE id = %s", (data.get('id'),))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/update_zone_location', methods=['POST'])
def update_zone_location():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE zones SET coords = %s WHERE id = %s", (json.dumps(data.get('coords')), data.get('id')))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/get_routes')
def get_routes():
    try:
        user = request.args.get('user', ''); conn = get_db_connection()
        if not conn: return jsonify([]), 200
        c = conn.cursor()
        if user: c.execute("SELECT id, coords, comment, temporary, assigned_to FROM routes WHERE temporary = FALSE AND (assigned_to = %s OR assigned_to IS NULL)", (user,))
        else: c.execute("SELECT id, coords, comment, temporary, assigned_to FROM routes WHERE temporary = FALSE")
        routes = [{"id": r[0], "coords": json.loads(r[1]) if r[1] else [], "comment": r[2] or "", "assigned_to": r[4] if len(r) > 4 else None} for r in c.fetchall()]
        conn.close(); return jsonify(routes)
    except Exception: return jsonify([]), 200

@app.route('/get_temp_routes')
def get_temp_routes():
    try:
        conn = get_db_connection(); c = conn.cursor(); c.execute("SELECT id, coords, comment FROM routes WHERE temporary = TRUE")
        routes = [{"id": r[0], "coords": json.loads(r[1]) if r[1] else [], "comment": r[2] or ""} for r in c.fetchall()]
        conn.close(); return jsonify(routes)
    except Exception: return jsonify([]), 200

@app.route('/clear_temp_routes', methods=['POST'])
def clear_temp_routes():
    try:
        conn = get_db_connection(); c = conn.cursor(); c.execute("DELETE FROM routes WHERE temporary = TRUE"); count = c.rowcount; conn.commit(); conn.close()
        return jsonify({"success": True, "count": count})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/add_route', methods=['POST'])
def add_route():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor()
        temp_value = data.get('temporary', False)
        if isinstance(temp_value, str): temp_value = temp_value.lower() == 'true'
        else: temp_value = bool(temp_value)
        c.execute("INSERT INTO routes (coords, comment, temporary) VALUES (%s, %s, %s)", (json.dumps(data.get('coords')), data.get('comment', ''), temp_value))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/delete_route', methods=['POST'])
def delete_route():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor(); c.execute("DELETE FROM routes WHERE id = %s", (data.get('id'),)); conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/update_route', methods=['POST'])
def update_route():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE routes SET coords = %s WHERE id = %s", (json.dumps(data.get('coords')), data.get('id')))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/assign_task', methods=['POST'])
def assign_task():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor()
        if data.get('type') == 'point': c.execute("UPDATE points SET assigned_to = %s WHERE id = %s", (data.get('user'), data.get('id')))
        elif data.get('type') == 'zone': c.execute("UPDATE zones SET assigned_to = %s WHERE id = %s", (data.get('user'), data.get('id')))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/assign_route', methods=['POST'])
def assign_route():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE routes SET assigned_to = %s WHERE id = %s", (data.get('user'), data.get('id')))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/clear_assignments', methods=['POST'])
def clear_assignments():
    try:
        data = request.json; user = data.get('user'); conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE points SET assigned_to = NULL WHERE assigned_to = %s", (user,))
        c.execute("UPDATE zones SET assigned_to = NULL WHERE assigned_to = %s", (user,))
        c.execute("UPDATE routes SET assigned_to = NULL WHERE assigned_to = %s", (user,))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/get_departures')
def get_departures():
    try:
        conn = get_db_connection(); c = conn.cursor(); c.execute("SELECT point_id, point_title, person_name FROM departures")
        departures = [{"point_id": r[0], "point_title": r[1], "person_name": r[2]} for r in c.fetchall()]; conn.close()
        return jsonify(departures)
    except Exception: return jsonify([]), 200

@app.route('/add_departure', methods=['POST'])
def add_departure():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor()
        c.execute("""INSERT INTO departures (point_id, point_title, person_name) VALUES (%s, %s, %s) ON CONFLICT (point_id) DO UPDATE SET point_title = EXCLUDED.point_title, person_name = EXCLUDED.person_name""", 
                  (data.get('point_id'), data.get('point_title'), data.get('person_name')))
        conn.commit(); conn.close(); return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

@app.route('/remove_departure', methods=['POST'])
def remove_departure():
    try:
        data = request.json; conn = get_db_connection(); c = conn.cursor(); c.execute("DELETE FROM departures WHERE point_id = %s", (data.get('point_id'),)); conn.commit(); conn.close()
        return jsonify({"success": True})
    except Exception: return jsonify({"error": "DB failed"}), 500

def stop_bot_safely():
    global bot_running; bot_running = False
    try: bot.stop_polling()
    except: pass

def run_bot():
    global bot_running, startup_notification_sent
    if bot_running: return
    print("🔄 Инициализация БД и загрузка мониторингов...")
    init_db(); startup_notification_sent = False; load_monitors_from_db()
    log_to_owner("✅ Я начал мониторить новые самокаты из Поиска (топик 8694).")
    bot_running = True; print("🤖 Запускаю бота...")
    try: bot.remove_webhook()
    except: pass
    time.sleep(15)
    while bot_running:
        try: bot.polling(none_stop=True, timeout=120, long_polling_timeout=120, allowed_updates=["message"])
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 409:
                try: bot.stop_polling()
                except: pass
                time.sleep(90)
            else: time.sleep(30)
        except Exception:
            try: bot.stop_polling()
            except: pass
            time.sleep(30)

def handle_signal(signum, frame):
    stop_bot_safely(); sys.exit(0)

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

if __name__ == '__main__':
    print("🚀 Запуск сервера...")
    bot_thread = threading.Thread(target=run_bot, daemon=True); bot_thread.start()
    app.run(host='0.0.0.0', port=8080, threaded=True)
