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

# ✅ ХРАНИЛИЩЕ МОНИТОРИНГОВ (в памяти)
active_monitors = {}
active_call_monitors = {}

# ✅ СЛОВАРЬ ДЛЯ ПОДТВЕРЖДЕНИЯ ДЕЙСТВИЙ
pending_confirmations = {}

# ✅ ЖЁСТКО ЗАДАННЫЕ ЗНАЧЕНИЯ
DEFAULT_GROUP_ID = -1003920057901
DEFAULT_FZ_THREAD_ID = 79227
DEFAULT_CALL_THREAD_ID = 22277

# ✅ ТВОЙ ЛИЧНЫЙ USER_ID ДЛЯ УВЕДОМЛЕНИЙ
OWNER_USER_ID = 7929131842

# ✅ ТОПИК ДЛЯ АВТО-СОЗДАНИЯ САМОКАТОВ + КАРТОЧКИ ТГ
TGK_THREAD_ID = 8694

# ✅ ID группы и топиков
monitored_group_id = DEFAULT_GROUP_ID
monitored_thread_id = DEFAULT_FZ_THREAD_ID
call_thread_id = DEFAULT_CALL_THREAD_ID

# ✅ ID пользователя для уведомлений (fallback)
monitored_user_id = None

# ✅ Флаг для отправки уведомления о пробуждении
startup_notification_sent = False

# ✅ ФУНКЦИЯ ДЛЯ ЛОГИРОВАНИЯ ПРЯМО В TELEGRAM
def log_to_owner(text, parse_mode=None):
    try:
        bot.send_message(OWNER_USER_ID, text, parse_mode=parse_mode)
    except Exception as e:
        print(f"❌ Ошибка отправки лога в ЛС: {e}")

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
        print(f"❌ Ошибка подключения к БД: {e}")
        return None

def column_exists(c, table, column):
    try:
        c.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name='{table}' AND column_name='{column}'")
        return c.fetchone() is not None
    except:
        return False

# ✅ СОХРАНЕНИЕ МОНИТОРИНГОВ В БД
def save_monitors_to_db():
    try:
        conn = get_db_connection()
        if not conn: return
        c = conn.cursor()
        c.execute("DELETE FROM active_monitors")
        c.execute("DELETE FROM active_call_monitors")
        for key, monitor in active_monitors.items():
            c.execute("""INSERT INTO active_monitors 
                        (key, chat_id, thread_id, last_three, user_id, start_time, active)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                     (key, monitor['chat_id'], monitor.get('thread_id'), 
                      monitor['last_three'], monitor['user_id'],
                      monitor['start_time'].isoformat(), monitor['active']))
        for key, monitor in active_call_monitors.items():
            c.execute("""INSERT INTO active_call_monitors
                        (key, chat_id, thread_id, user_id, start_time, active)
                        VALUES (%s, %s, %s, %s, %s, %s)""",
                     (key, monitor['chat_id'], monitor.get('thread_id'),
                      monitor['user_id'], monitor['start_time'].isoformat(), monitor['active']))
        
        all_monitors = list(active_monitors.values()) + list(active_call_monitors.values())
        if all_monitors:
            last_user_id = all_monitors[-1].get('user_id')
            if last_user_id:
                c.execute("""INSERT INTO bot_settings (key, value) VALUES 
                            ('monitored_user_id', %s)
                            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                         (str(last_user_id),))
        
        c.execute("""INSERT INTO bot_settings (key, value) VALUES 
                    ('monitored_group_id', %s),
                    ('monitored_thread_id', %s),
                    ('call_thread_id', %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                 (str(monitored_group_id), str(monitored_thread_id), str(call_thread_id)))
        conn.commit()
        conn.close()
        print(f"✅ Сохранено в БД: {len(active_monitors)} ФЗ, {len(active_call_monitors)} обзвонов")
    except Exception as e:
        print(f"❌ Ошибка сохранения в БД: {e}")
        traceback.print_exc()

# ✅ ЗАГРУЗКА МОНИТОРИНГОВ ИЗ БД
def load_monitors_from_db():
    global monitored_group_id, monitored_thread_id, call_thread_id, startup_notification_sent, monitored_user_id
    try:
        conn = get_db_connection()
        if not conn: 
            print("❌ Нет подключения к БД при загрузке")
            return
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
                
        # ✅ ЗАГРУЗКА ФЗ МОНИТОРИНГОВ (с обнулением start_time)
        c.execute("SELECT key, chat_id, thread_id, last_three, user_id, start_time, active FROM active_monitors")
        for row in c.fetchall():
            key, chat_id, thread_id, last_three, user_id, start_time_str, active = row
            if active:
                active_monitors[key] = {
                    'chat_id': chat_id, 
                    'thread_id': thread_id, 
                    'last_three': last_three,
                    'user_id': user_id, 
                    'start_time': datetime.now(),
                    'active': True
                }
        
        # ✅ ЗАГРУЗКА ОБЗВОНОВ (с обнулением start_time)
        c.execute("SELECT key, chat_id, thread_id, user_id, start_time, active FROM active_call_monitors")
        for row in c.fetchall():
            key, chat_id, thread_id, user_id, start_time_str, active = row
            if active:
                active_call_monitors[key] = {
                    'chat_id': chat_id, 
                    'thread_id': thread_id,
                    'user_id': user_id, 
                    'start_time': datetime.now(),
                    'active': True
                }
        
        conn.close()
        fz_count = len(active_monitors)
        call_count = len(active_call_monitors)
        print(f"✅ Загружено из БД: {fz_count} ФЗ, {call_count} обзвонов")
        print(f"   Group ID: {monitored_group_id}")
        print(f"   ФЗ Thread ID: {monitored_thread_id}")
        print(f"   Обзвоны Thread ID: {call_thread_id}")
        print(f"   OWNER_USER_ID: {OWNER_USER_ID}")

        # ✅ АВТОЗАПУСК МОНИТОРИНГА ОБЗВОНОВ (постоянный)
        call_monitor_key = f"{monitored_group_id}_{call_thread_id}_call"
        if call_monitor_key not in active_call_monitors:
            target_user = monitored_user_id
            if not target_user and active_monitors:
                target_user = list(active_monitors.values())[0].get('user_id')
            if not target_user:
                target_user = OWNER_USER_ID
            
            print(f"🔍 Автозапуск обзвонов: target_user={target_user}")
            
            if target_user:
                active_call_monitors[call_monitor_key] = {
                    'chat_id': monitored_group_id,
                    'thread_id': call_thread_id,
                    'user_id': target_user,
                    'start_time': datetime.now(),
                    'active': True
                }
                save_monitors_to_db()
                print(f"✅ Автозапуск мониторинга обзвонов (топик {call_thread_id}, user {target_user})")
                call_count = len(active_call_monitors)

        # ✅ ОТПРАВКА УВЕДОМЛЕНИЯ О ПРОБУЖДЕНИИ (ВСЕГДА)
        if not startup_notification_sent:
            print(f"🔍 Проверка startup_notification_sent: {startup_notification_sent}")
            
            msg = (
                f"🤖 Я проснулся!\n\n"
                f"✅ Мониторинги перезапущены.\n"
                f"📊 Сейчас активно:\n"
                f"• ФЗ: {fz_count}\n"
                f"• Обзвоны: {call_count}"
            )
            
            try:
                bot.send_message(OWNER_USER_ID, msg)
                print(f"✅✅✅ Уведомление 'Я проснулся!' отправлено тебе (ID: {OWNER_USER_ID})")
            except Exception as e:
                print(f"❌❌❌ Ошибка отправки уведомления OWNER_USER_ID: {e}")
                traceback.print_exc()
            
            users_notified = {OWNER_USER_ID}
            all_monitors = list(active_monitors.values()) + list(active_call_monitors.values())
            for monitor in all_monitors:
                user_id = monitor.get('user_id')
                if user_id and user_id not in users_notified:
                    try:
                        bot.send_message(user_id, msg)
                        users_notified.add(user_id)
                        print(f"✅ Доп. уведомление отправлено пользователю {user_id}")
                    except Exception as e:
                        print(f"❌ Ошибка уведомления {user_id}: {e}")
            
            startup_notification_sent = True
            print(f"📨 Всего уведомлений отправлено: {len(users_notified)}")

    except Exception as e:
        print(f"❌ Ошибка загрузки из БД: {e}")
        traceback.print_exc()

def init_db():
    global db_initialized
    if db_initialized: return
    print("🔄 Инициализация БД...")
    try:
        conn = get_db_connection()
        if not conn:
            print("❌ Нет подключения к БД")
            return
        c = conn.cursor()
        print("📦 Создаю таблицы...")
        c.execute('''CREATE TABLE IF NOT EXISTS points
                     (id SERIAL PRIMARY KEY, title TEXT, address TEXT, lat REAL, lon REAL, 
                      status TEXT DEFAULT 'active', comment TEXT, url TEXT, tgk_url TEXT)''')
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
        c.execute('''CREATE TABLE IF NOT EXISTS active_monitors
                     (key TEXT PRIMARY KEY, chat_id BIGINT, thread_id BIGINT, 
                      last_three TEXT, user_id BIGINT, start_time TIMESTAMP, active BOOLEAN)''')
        c.execute('''CREATE TABLE IF NOT EXISTS active_call_monitors
                     (key TEXT PRIMARY KEY, chat_id BIGINT, thread_id BIGINT,
                      user_id BIGINT, start_time TIMESTAMP, active BOOLEAN)''')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_settings
                     (key TEXT PRIMARY KEY, value TEXT)''')
        conn.commit()
        print("✅ Таблицы созданы")
        print("🔧 Проверяю колонки...")
        if not column_exists(c, 'points', 'comment'): c.execute("ALTER TABLE points ADD COLUMN comment TEXT"); conn.commit()
        if not column_exists(c, 'points', 'url'): c.execute("ALTER TABLE points ADD COLUMN url TEXT"); conn.commit()
        if not column_exists(c, 'points', 'assigned_to'): c.execute("ALTER TABLE points ADD COLUMN assigned_to TEXT"); conn.commit()
        if not column_exists(c, 'points', 'tgk_url'): c.execute("ALTER TABLE points ADD COLUMN tgk_url TEXT"); conn.commit()
        if not column_exists(c, 'parkings', 'photo_url'): c.execute("ALTER TABLE parkings ADD COLUMN photo_url TEXT"); conn.commit()
        if not column_exists(c, 'zones', 'assigned_to'): c.execute("ALTER TABLE zones ADD COLUMN assigned_to TEXT"); conn.commit()
        if not column_exists(c, 'routes', 'temporary'): c.execute("ALTER TABLE routes ADD COLUMN temporary BOOLEAN DEFAULT FALSE"); conn.commit()
        if not column_exists(c, 'routes', 'assigned_to'): c.execute("ALTER TABLE routes ADD COLUMN assigned_to TEXT"); conn.commit()
        for key, value in [('hide_empty_labels', 'false'), ('show_parkings', 'true'), ('show_labels', 'true')]:
            c.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (key, value))
        conn.commit()
        db_initialized = True
        print("✅ База данных инициализирована!")
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
        print(f"❌ Ошибка trash: {e}")

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
            c.execute("""INSERT INTO points (title, address, lat, lon, status, comment, url, assigned_to, tgk_url)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                     (data.get('title'), data.get('address'), data.get('lat'), data.get('lon'),
                      data.get('status', 'active'), data.get('comment', ''), data.get('url', ''),
                      data.get('assigned_to'), data.get('tgk_url', '')))
        elif obj_type == 'zone':
            c.execute("""INSERT INTO zones (coords, comment, assigned_to)
                        VALUES (%s, %s, %s) RETURNING id""",
                     (data.get('coords'), data.get('comment', ''), data.get('assigned_to')))
        elif obj_type == 'parking':
            c.execute("""INSERT INTO parkings (title, address, lat, lon, available_count, dead_count, broken_count, photo_url)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                     (data.get('title'), data.get('address'), data.get('lat'), data.get('lon'),
                      data.get('available_count', '?'), data.get('dead_count', '?'),
                      data.get('broken_count', '?'), data.get('photo_url')))
        else:
            conn.close()
            return None, f"Неизвестный тип: {obj_type}"
        new_id = c.fetchone()[0]
        c.execute("DELETE FROM trash WHERE id = %s", (trash_id,))
        conn.commit(); conn.close()
        return f"✅ Восстановлено (ID: {new_id})", None
    except Exception as e:
        print(f"❌ Ошибка восстановления: {e}")
        traceback.print_exc()
        return None, str(e)

def build_address_queries(address):
    a = ' '.join(address.split()).strip()
    a = re.sub(r'^(г\.?\s*)?Смоленск\s*,?\s*', '', a, flags=re.IGNORECASE).strip()
    m = re.match(r'^(?:улица|ул\.?|проспект|пр-кт|переулок|пер\.?|бульвар|б-р)?\s*([а-яА-ЯёЁ\w\-]+)\s*,?\s*(\d+[А-Яа-яЁё]?)$', a, re.IGNORECASE)
    
    if m:
        street = m.group(1)
        house = m.group(2)
        variants = [
            f"Смоленск, улица {street}, {house}",
            f"г. Смоленск, улица {street}, {house}",
            f"Смоленск, улица {street} {house}",
            f"Смоленск, {street}, {house}",
            f"Смоленск, {street} {house}",
            f"Смоленская область, Смоленск, улица {street}, {house}",
            f"Смоленск, улица {street}",
            f"Смоленск, {street}",
        ]
    else:
        variants = [
            f"Смоленск, {a}",
            f"г. Смоленск, {a}",
            f"Смоленская область, Смоленск, {a}",
            f"Смоленск, улица {a}",
        ]
    
    result = []
    for v in variants:
        if v not in result:
            result.append(v)
    return result

def get_coordinates(address):
    variants = build_address_queries(address)
    url = "https://nominatim.openstreetmap.org/search"
    headers = {"User-Agent": "SmolenskMapBot/1.0 (contact: telegram)"}
    
    for i, query in enumerate(variants):
        if i > 0:
            time.sleep(1.1)
        try:
            response = requests.get(url, params={"q": query, "format": "json", "limit": 1}, headers=headers, timeout=8)
            if response.status_code == 429:
                time.sleep(3)
                continue
            if response.status_code != 200:
                continue
            data = response.json()
            if data and 'lat' in data[0] and 'lon' in data[0]:
                return float(data[0]["lat"]), float(data[0]["lon"])
        except Exception:
            continue
    return None, None

def parse_scooter_command(text):
    t = ' '.join(text.split()).strip()
    m = re.match(r'^(.+?)\s*-\s*(\d{3})(?:\s+(.*))?$', t, re.IGNORECASE)
    if not m:
        return None
    location = m.group(1).strip()
    last_three = m.group(2)
    rest = (m.group(3) or "").strip()
    if rest.lower().endswith('p') or rest.lower() == 'удалить':
        return None
    return location, last_three, rest

def normalize_address(raw_address):
    if re.match(r'^-?\d+\.\d+\s*,\s*-?\d+\.\d+$', raw_address.strip()):
        return raw_address.strip()
    
    a = raw_address.strip()
    a = re.sub(r'^г\.?\s*Смоленск\s*,?\s*', '', a, flags=re.IGNORECASE).strip()
    a = re.sub(r',?\s*(?:д\.?|дом\.?)\s*', ' ', a, flags=re.IGNORECASE).strip()
    a = re.sub(r',\s*', ' ', a)
    a = re.sub(r'\s+', ' ', a).strip()
    
    m = re.match(r'^(ул\.?|улица|пр-кт|проспект|переулок|пер\.?|бульвар|б-р)\.?\s+(.+?)\s+(\d+[А-Яа-яЁё]?)$', a, re.IGNORECASE)
    if m:
        return f"{m.group(2).strip()} {m.group(3).strip()}"
    
    m = re.match(r'^(.+?)\s+(ул\.?|улица|пр-кт|проспект|переулок|пер\.?|бульвар|б-р)\.?\s+(\d+[А-Яа-яЁё]?)$', a, re.IGNORECASE)
    if m:
        return f"{m.group(1).strip()} {m.group(3).strip()}"
    
    m = re.match(r'^([а-яА-ЯёЁ\w\-]+(?:\s+[а-яА-ЯёЁ\w\-]+)*)\s+(\d+[А-Яа-яЁё]?)$', a)
    if m:
        return f"{m.group(1).strip()} {m.group(2).strip()}"
    
    return a if len(a) > 3 else None

def parse_scooter_message_8694(text):
    last_three = None
    status = 'sev'
    
    scooter_match = re.search(r'(?:🛴\s*)?7703(\d{3})\b', text)
    if not scooter_match:
        return None
    last_three = scooter_match.group(1)
    
    address = None
    
    match = re.search(r'Самокат GPS:\s*(.+?)(?:\n|$)', text, re.IGNORECASE)
    if match:
        address = match.group(1).strip()
    
    if not address:
        match = re.search(r'Текущий gps:\s*(.+?)(?:\n|$)', text, re.IGNORECASE)
        if match:
            address = match.group(1).strip()
    
    if not address:
        match = re.search(r'г\.?\s*Смоленск,\s*((?:ул\.?|улица|пр-кт|проспект|переулок|пер\.?)\s+[^,\n]+(?:,\s*(?:д\.?\s*)?\d+[А-Яа-яЁё]?)?)', text, re.IGNORECASE)
        if match:
            address = match.group(1).strip()
    
    if not address and re.search(r'Нет координат в пределах города', text, re.IGNORECASE):
        address = "54.776443, 32.050324"
    
    if not address:
        return None
    
    address = normalize_address(address)
    if not address:
        return None
    
    return address, last_three, status

def create_scooter_only(address, last_three, status, tgk_url):
    try:
        conn = get_db_connection()
        if not conn:
            return False, "Нет подключения к БД"
        
        c = conn.cursor()
        c.execute("SELECT id, tgk_url FROM points WHERE title = %s AND status != 'found'", (last_three,))
        existing = c.fetchone()
        
        if existing:
            existing_id, existing_tgk = existing
            if not existing_tgk and tgk_url:
                c.execute("UPDATE points SET tgk_url = %s WHERE id = %s", (tgk_url, existing_id))
                conn.commit()
            conn.close()
            return True, "Самокат уже существовал (карточка обновлена или была)"
        
        c.execute("""INSERT INTO points (title, address, lat, lon, status, comment, url, tgk_url) 
                    VALUES (%s, %s, NULL, NULL, %s, '', '', %s)""", 
                 (last_three, address, status, tgk_url))
        conn.commit()
        
        c.execute("SELECT id FROM points WHERE title = %s AND status != 'found'", (last_three,))
        created = c.fetchone()
        
        if not created:
            conn.close()
            return False, "INSERT выполнен, но запись не найдена в БД"
        
        conn.close()
        return True, "Успешно создан"
        
    except Exception as e:
        return False, str(e)

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

# --- КОМАНДЫ УПРАВЛЕНИЯ ТРЕДАМИ ---
@bot.message_handler(regexp=r'^удали тред айди$')
def delete_thread_id(message):
    global monitored_thread_id, call_thread_id
    if message.chat.type != 'private': return
    monitored_thread_id = DEFAULT_FZ_THREAD_ID
    call_thread_id = DEFAULT_CALL_THREAD_ID
    save_monitors_to_db()
    bot.reply_to(message, f"✅ Thread ID сброшены:\nФЗ: {DEFAULT_FZ_THREAD_ID}\nОбзвоны: {DEFAULT_CALL_THREAD_ID}")

@bot.message_handler(regexp=r'^тред обзвон off$')
def reset_call_thread(message):
    global call_thread_id
    if message.chat.type != 'private': return
    call_thread_id = DEFAULT_CALL_THREAD_ID
    save_monitors_to_db()
    bot.reply_to(message, f"✅ Обзвоны сброшены: {DEFAULT_CALL_THREAD_ID}")

@bot.message_handler(regexp=r'^тред фз off$')
def reset_fz_thread(message):
    global monitored_thread_id
    if message.chat.type != 'private': return
    monitored_thread_id = DEFAULT_FZ_THREAD_ID
    save_monitors_to_db()
    bot.reply_to(message, f"✅ ФЗ сброшен: {DEFAULT_FZ_THREAD_ID}")

@bot.message_handler(regexp=r'^тред фз (\d+)$')
def set_fz_thread(message):
    global monitored_thread_id
    if message.chat.type != 'private': return
    monitored_thread_id = int(message.matches[0].group(1))
    save_monitors_to_db()
    bot.reply_to(message, f"✅ ФЗ тред установлен: {monitored_thread_id}")

@bot.message_handler(regexp=r'^тред обзвон (\d+)$')
def set_call_thread(message):
    global call_thread_id
    if message.chat.type != 'private': return
    call_thread_id = int(message.matches[0].group(1))
    save_monitors_to_db()
    bot.reply_to(message, f"✅ Обзвоны тред установлен: {call_thread_id}")

@bot.message_handler(regexp=r'^тредайдикакой\?$')
def show_thread_id(message):
    user_id = message.from_user.id
    if message.chat.type == 'private':
        response = f"📊 Текущие настройки:\n\n"
        response += f"💬 Группа ID: {monitored_group_id}\n"
        response += f"📂 ФЗ Thread ID: {monitored_thread_id}\n"
        response += f"📞 Обзвоны Thread ID: {call_thread_id}\n"
        response += f"🔗 TGK авто Thread ID: {TGK_THREAD_ID}"
        bot.send_message(user_id, response)
    elif message.chat.type in ['group', 'supergroup']:
        thread_id = getattr(message, 'message_thread_id', None)
        if thread_id:
            response = f"📂 ID этого треда: {thread_id}\n\n"
            response += f"💬 Группа ID: {message.chat.id}\n\n"
            response += f"Чтобы задать для ФЗ, напиши в ЛС:\nтред фз {thread_id}\n\n"
            response += f"Чтобы задать для обзвонов:\nтред обзвон {thread_id}"
            bot.send_message(user_id, response)
        else:
            response = f"⚠️ Это общий чат, не тред.\n\n💬 Группа ID: {message.chat.id}\n\nНапиши команду внутри треда."
            bot.send_message(user_id, response)

@bot.message_handler(commands=['мой_айди', 'myid'])
def show_my_id(message):
    if message.chat.type != 'private':
        bot.reply_to(message, "Эта команда работает только в ЛС!")
        return
    user_id = message.from_user.id
    bot.reply_to(message, 
        f"🆔 Твой user_id: `{user_id}`\n\n"
        f"OWNER_USER_ID в коде: `{OWNER_USER_ID}`\n"
        f"Текущий monitored_user_id: `{monitored_user_id}`",
        parse_mode='Markdown')

# --- МОНИТОРИНГ ФЗ ---
@bot.message_handler(commands=['мониторь'])
def start_monitor(message):
    text = message.text.strip()
    parts = text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Использование: /мониторь <последние_3_цифры>\nПример: /мониторь 775")
        return
    last_three = parts[1]
    if not last_three.isdigit() or len(last_three) != 3:
        bot.reply_to(message, "Нужно ровно 3 цифры!")
        return
    user_id = message.from_user.id
    chat_id = monitored_group_id
    thread_id = monitored_thread_id
    monitor_key = f"{chat_id}_{thread_id}_{last_three}"
    if monitor_key in active_monitors and active_monitors[monitor_key]['active']:
        bot.reply_to(message, f"⚠️ Мониторинг 7703{last_three} уже активен!")
        return
    start_time = datetime.now()
    active_monitors[monitor_key] = {
        'chat_id': chat_id, 'thread_id': thread_id, 'last_three': last_three,
        'user_id': user_id, 'start_time': start_time, 'active': True
    }
    save_monitors_to_db()
    bot.reply_to(message, 
        f"✅ Мониторинг запущен!\n\n"
        f"🔍 Ищу: 7703{last_three}\n"
        f"💬 Группа ID: {chat_id}\n"
        f"📂 Раздел (thread): {thread_id}\n"
        f"⏰ С: {start_time.strftime('%d.%m.%Y %H:%M:%S')}\n"
        f"📩 Уведомления в ЛС")

@bot.message_handler(commands=['стоп'])
def stop_monitor(message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(message, "Использование: /стоп <последние_3_цифры>")
        return
    last_three = parts[1]
    monitor_key = f"{monitored_group_id}_{monitored_thread_id}_{last_three}"
    if monitor_key in active_monitors:
        del active_monitors[monitor_key]
        save_monitors_to_db()
        bot.reply_to(message, f"❌ Мониторинг 7703{last_three} остановлен")
    else:
        bot.reply_to(message, "Мониторинг не найден")

@bot.message_handler(commands=['мониторы'])
def list_monitors(message):
    user_id = message.from_user.id
    user_monitors = {k: v for k, v in active_monitors.items() if v['user_id'] == user_id and v['active']}
    call_monitor_key = f"{monitored_group_id}_{call_thread_id}_call"
    call_monitor_active = call_monitor_key in active_call_monitors and active_call_monitors[call_monitor_key]['active']
    
    text = "📊 Все мониторинги:\n\n"
    text += "🔍 ФЗ мониторинги:\n"
    if user_monitors:
        for key, monitor in user_monitors.items():
            thread_info = f" (топик {monitor['thread_id']})" if monitor['thread_id'] else ""
            text += f"  • 7703{monitor['last_three']}{thread_info}\n"
    else:
        text += "  (нет активных)\n"
    
    text += "\n📞 Обзвоны:\n"
    text += f"  ✅ {'ПОСТОЯННО АКТИВЕН' if call_monitor_active else 'НЕ АКТИВЕН'} (топик {call_thread_id})\n"
    
    text += "\n🔗 Авто-создание самокатов + карточки ТГ:\n"
    text += f"  ✅ Всегда активен (топик {TGK_THREAD_ID})\n"
    
    text += "\n🗑️ Авто-удаление самокатов:\n"
    text += f"  ✅ Всегда активен (топик {call_thread_id})\n"
    
    text += "\n📋 Авто-уведомление об аренде:\n"
    text += f"  ✅ Всегда активен (топик {call_thread_id})\n"
    
    bot.reply_to(message, text)

@bot.message_handler(commands=['off'])
def off_monitors(message):
    text = message.text.strip()
    if 'все мониторинги' in text.lower() or 'все мониторы' in text.lower():
        user_id = message.from_user.id
        keys_to_delete = [k for k, v in active_monitors.items() if v['user_id'] == user_id and v['active']]
        if not keys_to_delete:
            bot.reply_to(message, "❌ У тебя нет активных мониторингов")
            return
        for key in keys_to_delete:
            del active_monitors[key]
        save_monitors_to_db()
        bot.reply_to(message, f"🗑️ Удалено мониторингов: {len(keys_to_delete)}")
        return
    elif 'last мониторинг' in text.lower() or 'последний мониторинг' in text.lower():
        user_id = message.from_user.id
        user_monitors = {k: v for k, v in active_monitors.items() if v['user_id'] == user_id and v['active']}
        if not user_monitors:
            bot.reply_to(message, "❌ У тебя нет активных мониторингов")
            return
        last_key = max(user_monitors.keys(), key=lambda k: user_monitors[k]['start_time'])
        last_three = user_monitors[last_key]['last_three']
        del active_monitors[last_key]
        save_monitors_to_db()
        bot.reply_to(message, f"🗑️ Последний мониторинг 7703{last_three} удалён")
        return
    else:
        bot.reply_to(message, "Использование:\n/off все мониторинги\n/off last мониторинг")
        return

# --- МОНИТОРИНГ ОБЗВОНОВ ---
@bot.message_handler(commands=['обзвоны'])
def start_call_monitor(message):
    user_id = message.from_user.id
    call_monitor_key = f"{monitored_group_id}_{call_thread_id}_call"
    if call_monitor_key not in active_call_monitors:
        active_call_monitors[call_monitor_key] = {
            'chat_id': monitored_group_id, 'thread_id': call_thread_id,
            'user_id': user_id, 'start_time': datetime.now(), 'active': True
        }
        save_monitors_to_db()
    bot.reply_to(message, "✅ Мониторинг обзвонов активен!")

@bot.message_handler(commands=['стоп_обзвоны'])
def stop_call_monitor(message):
    monitor_key = f"{monitored_group_id}_{call_thread_id}_call"
    if monitor_key in active_call_monitors:
        del active_call_monitors[monitor_key]
        save_monitors_to_db()
        bot.reply_to(message, "❌ Мониторинг обзвонов остановлен")
    else:
        bot.reply_to(message, "Мониторинг обзвонов не найден")

# --- ОСНОВНОЙ ОБРАБОТЧИК ---
@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if message.text is None:
        return
    text = message.text.strip()
    text_lower = text.lower()
    user_id = message.from_user.id

    if message.chat.type == 'private':
        global monitored_user_id
        if monitored_user_id != user_id:
            monitored_user_id = user_id
            try:
                conn = get_db_connection()
                if conn:
                    c = conn.cursor()
                    c.execute("""INSERT INTO bot_settings (key, value) VALUES 
                                ('monitored_user_id', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (str(user_id),))
                    conn.commit(); conn.close()
            except Exception:
                pass

    if user_id in pending_confirmations:
        if text_lower == 'да':
            action = pending_confirmations.pop(user_id)
            if action == 'delete_all_scooters':
                try:
                    conn = get_db_connection()
                    if conn:
                        c = conn.cursor(); c.execute("DELETE FROM points"); count = c.rowcount; conn.commit(); conn.close()
                        bot.reply_to(message, f"🗑️ Удалено всех самокатов: {count}")
                except Exception as e:
                    bot.reply_to(message, f"❌ Ошибка: {e}")
        else:
            pending_confirmations.pop(user_id)
            bot.reply_to(message, "❌ Отменено.")
        return

    if message.chat.type in ['group', 'supergroup']:
        msg_thread_id = getattr(message, 'message_thread_id', None)
        message_time = datetime.fromtimestamp(message.date)
        
        # 1. ФЗ Мониторинг
        for key, monitor in list(active_monitors.items()):
            if monitor['chat_id'] == monitored_group_id and monitor['active']:
                if monitor.get('thread_id') is not None and msg_thread_id != monitor['thread_id']: continue
                if message_time < monitor['start_time']: continue
                pattern = f"7703{monitor['last_three']}"
                if pattern in message.text:
                    try:
                        raw_chat_id = str(monitored_group_id)
                        chat_id_for_link = raw_chat_id[4:] if raw_chat_id.startswith('-100') else raw_chat_id.lstrip('-')
                        message_link = f"https://t.me/c/{chat_id_for_link}/{message.message_id}"
                        bot.send_message(monitor['user_id'], f"Произошло новое ФЗ {monitor['last_three']} !\n\n🔗 {message_link}", disable_web_page_preview=True)
                        del active_monitors[key]; save_monitors_to_db()
                    except Exception as e:
                        print(f"❌ Ошибка ФЗ: {e}")

        # 2. Обзвоны Мониторинг
        for key, monitor in list(active_call_monitors.items()):
            if monitor['chat_id'] == monitored_group_id and monitor['active']:
                if monitor.get('thread_id') is not None and msg_thread_id != monitor['thread_id']: continue
                if 'Добавлен результат обзвона' in message.text:
                    try:
                        bot.forward_message(monitor['user_id'], monitored_group_id, message.message_id)
                    except Exception as e:
                        print(f"❌ Ошибка пересылки обзвона: {e}")

        # ==============================================================================
        # ⚠️⚠️⚠️ НЕЗАБУДЬКОД: АВТО-УДАЛЕНИЕ САМОКАТОВ ИЗ ПОИСКОВ ⚠️⚠️⚠️
        # Ищет: 'удален из Поисков' / 'удалён из Поиска'
        # Действие: удаляет самокат 7703*** из БД и пишет в ЛС
        # ==============================================================================
        msg_text_lower = message.text.lower()
        is_poisk_deleted = (
            msg_thread_id == call_thread_id and 
            ('из поис' in msg_text_lower) and 
            ('удален' in msg_text_lower or 'удалён' in msg_text_lower)
        )
        
        if is_poisk_deleted:
            print(f"🔍 Найдено сообщение о удалении из поиска: '{message.text}'")
            match = re.search(r'7703(\d{3})', message.text)
            if match:
                last_three = match.group(1)
                print(f"✅ Извлечён номер самоката: 7703{last_three}")
                try:
                    conn = get_db_connection()
                    if conn:
                        c = conn.cursor()
                        c.execute("DELETE FROM points WHERE title = %s", (last_three,))
                        deleted_count = c.rowcount
                        conn.commit()
                        conn.close()
                        
                        if deleted_count > 0:
                            print(f"🗑️ Самокат {last_three} удалён из БД")
                            try:
                                bot.send_message(OWNER_USER_ID, f"🗑️ {last_three} удалён с карты!")
                                print(f"✅ Уведомление отправлено тебе (ID: {OWNER_USER_ID})")
                            except Exception as e:
                                print(f"❌ Ошибка отправки уведомления: {e}")
                        else:
                            print(f"⚠️ Самокат {last_three} не найден в БД")
                            try:
                                bot.send_message(OWNER_USER_ID, f"⚠️ Самокат {last_three} не найден на карте!")
                            except Exception as e:
                                print(f"❌ Ошибка: {e}")
                except Exception as e:
                    print(f"❌ Ошибка удаления самоката: {e}")
                    traceback.print_exc()

        # ==============================================================================
        # ⚠️⚠️⚠️ НЕЗАБУДЬКОД: АВТО-УВЕДОМЛЕНИЕ ОБ АРЕНДЕ (БЕЗ АРЕНДЫ) ⚠️⚠️⚠️
        # Ищет: 'удален из Без аренды'
        # Действие: уведомление в ЛС '*** арендовал спустя 48+ часов'
        # ==============================================================================
        if msg_thread_id == call_thread_id and 'удален из без аренды' in msg_text_lower:
            match = re.search(r'7703(\d{3})', message.text)
            if match:
                last_three = match.group(1)
                print(f"✅ Найдено 'удален из Без аренды' с номером 7703{last_three}")
                try:
                    bot.send_message(OWNER_USER_ID, f"{last_three} кто-то арендовал спустя 48 часов или более.")
                    print(f"✅ Уведомление об аренде отправлено тебе (ID: {OWNER_USER_ID})")
                except Exception as e:
                    print(f"❌ Ошибка уведомления об аренде: {e}")

        # 3. Авто-создание самокатов из Топика 8694
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

    # ✅ ЛИЧНЫЕ СООБЩЕНИЯ
    tgk_match = re.match(r'^\s*(\d{3})\s+(https?://\S+)\s*$', text)
    if tgk_match:
        last_three, tgk_url = tgk_match.groups()
        try:
            conn = get_db_connection()
            if conn:
                c = conn.cursor()
                c.execute("UPDATE points SET tgk_url = %s WHERE title = %s", (tgk_url, last_three))
                updated_count = c.rowcount
                conn.commit()
                conn.close()
                if updated_count > 0:
                    bot.reply_to(message, f"✅ Ссылка добавлена к {updated_count} самокату(ам) с номером {last_three}.")
                else:
                    bot.reply_to(message, f"❌ Самокатов с номером {last_three} не найдено.")
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {e}")
        return

    if text_lower == 'z':
        result, error = restore_from_trash()
        if error:
            bot.reply_to(message, f"❌ {error}")
        else:
            bot.reply_to(message, result)
        return

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
                    if obj_type == 'point': msg += f"{i}. 🛴 Самокат '{data.get('title', '')}'\n"
                    elif obj_type == 'info': msg += f"{i}. ℹ️ Доп инфа '{data.get('title', '')}'\n"
                    elif obj_type == 'zone': msg += f"{i}. 🔴 Зона\n"
                    elif obj_type == 'parking': msg += f"{i}. 🅿️ Парковка '{data.get('title', '')}'\n"
                msg += "\nНапиши Z чтобы восстановить последнее"
                bot.reply_to(message, msg)
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {e}")
        return

    if text_lower.startswith('off ') and len(text_lower.split()) == 2:
        parts = text_lower.split()
        last_three = parts[1]
        if last_three.isdigit() and len(last_three) == 3:
            try:
                conn = get_db_connection()
                if conn:
                    c = conn.cursor()
                    c.execute("DELETE FROM points WHERE title = %s", (last_three,))
                    deleted_count = c.rowcount
                    conn.commit()
                    conn.close()
                    if deleted_count > 0:
                        bot.reply_to(message, f"🗑️ Самокат {last_three} удалён.")
                    else:
                        bot.reply_to(message, f"❌ Самокат {last_three} не найден.")
            except Exception as e:
                bot.reply_to(message, f"❌ Ошибка: {e}")
            return

    if text_lower == 'off last sam':
        try:
            conn = get_db_connection()
            if conn:
                c = conn.cursor()
                c.execute("DELETE FROM points WHERE id = (SELECT MAX(id) FROM points)")
                deleted_count = c.rowcount
                conn.commit()
                conn.close()
                if deleted_count > 0:
                    bot.reply_to(message, "🗑️ Последний самокат удалён.")
                else:
                    bot.reply_to(message, "❌ Самокатов нет.")
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {e}")
        return

    if text_lower == 'off all sam':
        try:
            conn = get_db_connection()
            if conn:
                c = conn.cursor()
                c.execute("SELECT COUNT(*) FROM points")
                count = c.fetchone()[0]
                conn.close()
                if count > 0:
                    pending_confirmations[user_id] = 'delete_all_scooters'
                    bot.reply_to(message, f"⚠️ Точно удалить все самокаты? ({count} шт.)\n\nНапиши 'Да' для подтверждения.")
                else:
                    bot.reply_to(message, "❌ Самокатов нет.")
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {e}")
        return

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
            bot.reply_to(message, "🗑️ Последний маршрут удалён.")
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
    
    if ' http' in text or ' https' in text or text.startswith('http') or text.startswith('https'):
        parts = text.split(' ', 1)
        if len(parts) == 2 and (parts[1].startswith('http') or parts[1].startswith('https')):
            name = parts[0].strip()
            if not re.match(r'^\d{3}$', name):
                photo_url = parts[1].strip()
                conn = get_db_connection(); c = conn.cursor()
                c.execute("UPDATE parkings SET photo_url = %s WHERE title = %s", (photo_url, name))
                conn.commit(); conn.close()
                bot.reply_to(message, f"✅ Ссылка на фото парковки '{name}' сохранена.")
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

    scooter_cmd = parse_scooter_command(text)
    if scooter_cmd:
        location_part, last_three, rest = scooter_cmd
        tgk_url, comment = "", ""
        
        tgk_m = re.search(r'\btgk\s+(https?://\S+)', rest, re.IGNORECASE)
        if tgk_m:
            tgk_url = tgk_m.group(1)
            rest = re.sub(r'\btgk\s+https?://\S+', '', rest, flags=re.IGNORECASE).strip()
        
        url_m = re.search(r'https?://\S+', rest)
        if url_m:
            fz_url = url_m.group(0)
            rest = rest.replace(fz_url, "").strip()
        
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
        else:
            lat, lon = get_coordinates(location_part)
            
        if lat is None or lon is None:
            bot.reply_to(message, f"⚠️ Не нашёл адрес '{location_part}'.\nПопробуй: улица Ленина, 11 - {last_three}")
            return
            
        try:
            conn = get_db_connection()
            if not conn:
                bot.reply_to(message, "❌ Нет подключения к БД")
                return
            c = conn.cursor()
            c.execute("""INSERT INTO points (title, address, lat, lon, status, comment, url, tgk_url) 
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                     (last_three, location_part, lat, lon, status, comment, '', tgk_url))
            conn.commit()
            conn.close()
            answer = f"✅ Самокат {last_three} добавлен!\n📍 {location_part}\n🎨 {status}"
            if comment: answer += f"\n💬 {comment}"
            if tgk_url: answer += "\n🔗 Карточка ТГ привязана"
            bot.reply_to(message, answer)
        except Exception as e:
            bot.reply_to(message, f"❌ Ошибка: {e}")
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
        else:
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
            c.execute("INSERT INTO points (title, address, lat, lon, status, comment, url, tgk_url) VALUES (%s, %s, %s, %s, 'info', %s, '', '')", 
                     ("ДОП ИНФА", location_part, lat, lon, comment))
            conn.commit(); conn.close()
            bot.reply_to(message, f"✅ Доп инфа добавлена!")
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
    if text_lower == 'last с off' or text_lower == 'last s off':
        conn = get_db_connection(); c = conn.cursor()
        c.execute("DELETE FROM departures WHERE point_id = (SELECT MAX(id) FROM points)")
        c.execute("DELETE FROM points WHERE id = (SELECT MAX(id) FROM points)")
        if c.rowcount > 0:
            bot.reply_to(message, "🗑️ Последний самокат удалён.")
        else:
            bot.reply_to(message, "❌ Самокатов нет.")
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
            c.execute("SELECT id, title, address, lat, lon, status, comment, url, assigned_to, tgk_url FROM points WHERE status != 'found' AND assigned_to = %s", (user,))
        else:
            c.execute("SELECT id, title, address, lat, lon, status, comment, url, assigned_to, tgk_url FROM points WHERE status != 'found'")
        points = [{"id": r[0], "title": r[1], "address": r[2], "lat": float(r[3]) if r[3] else 0, "lon": float(r[4]) if r[4] else 0, "status": r[5], "comment": r[6] or "", "url": r[7] or "", "assigned_to": r[8], "tgk_url": r[9] or ""} for r in c.fetchall()]
        conn.close()
        return jsonify(points)
    except Exception as e:
        return jsonify([]), 200

@app.route('/create_scooter', methods=['POST'])
def create_scooter():
    try:
        data = request.json
        conn = get_db_connection()
        if not conn: return jsonify({"error": "DB connection failed"}), 500
        c = conn.cursor()
        c.execute("INSERT INTO points (title, address, lat, lon, status, comment, url, tgk_url) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", 
                 (data.get('number', ''), data.get('address', ''), data.get('lat'), data.get('lon'), data.get('status', 'active'), data.get('comment', ''), data.get('url', ''), data.get('tgk_url', '')))
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
            c.execute("SELECT id, title, address, lat, lon, status, comment, url, assigned_to, tgk_url FROM points WHERE id = %s", (data.get('id'),))
            row = c.fetchone()
            if row:
                point_data = {'id': row[0], 'title': row[1], 'address': row[2], 'lat': row[3], 'lon': row[4],
                    'status': row[5], 'comment': row[6], 'url': row[7], 'assigned_to': row[8], 'tgk_url': row[9]}
                obj_type = 'info' if row[5] == 'info' else 'point'
                save_to_trash(obj_type, point_data)
                scooter_title = row[1]
                if obj_type == 'point':
                    try:
                        bot.send_message(OWNER_USER_ID, f"🎯 Кто-то нашёл самокат {scooter_title}!")
                    except Exception:
                        pass
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
        return jsonify(parkings)
    except Exception as e:
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
        return jsonify(zones)
    except Exception as e:
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
        return jsonify(routes)
    except Exception as e:
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
        return jsonify(routes)
    except Exception as e:
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
    print("🛑 Останавливаю бота...")
    bot_running = False
    try:
        bot.stop_polling()
        print("✅ Polling остановлен")
    except Exception as e:
        print(f"⚠️ Ошибка при остановке polling: {e}")

def run_bot():
    global bot_running, startup_notification_sent
    if bot_running:
        print("⚠️ Бот уже запущен, пропускаем")
        return
    
    print("🔄 Инициализация БД и загрузка мониторингов...")
    init_db()
    startup_notification_sent = False
    load_monitors_from_db()
    
    log_to_owner("✅ Я начал мониторить новые самокаты из Поиска (топик 8694).")
    
    bot_running = True
    print("🤖 Запускаю бота...")
    try:
        bot.remove_webhook()
        print("✅ Webhook очищен")
    except Exception as e:
        print(f"⚠️ Не удалось очистить webhook: {e}")
    print("⏳ Ожидаю 15 секунд...")
    time.sleep(15)
    while bot_running:
        try:
            print("🔄 Запускаю polling...")
            bot.polling(none_stop=True, timeout=120, long_polling_timeout=120, allowed_updates=["message"])
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 409:
                print("⚠️ Конфликт 409, жду 90 секунд...")
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
