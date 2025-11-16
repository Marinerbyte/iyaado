import asyncio
import json
import random
import time
import threading
import websockets
import requests
import os
import re
import secrets
from urllib.parse import quote_plus
from io import BytesIO
from flask import Flask, render_template_string, request, redirect, url_for, session
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from requests_toolbelt.multipart.encoder import MultipartEncoder
from bs4 import BeautifulSoup
from groq import Groq
import textwrap

# --- INICIO DE LA APLICACIÓN FLASK ---
app = Flask(__name__)
app.secret_key = os.getenv("SESSION_SECRET") or secrets.token_hex(32)

# --- CONFIGURACIÓN Y ESTADO GLOBAL DEL BOT ---
# Estos diccionarios serán compartidos entre el hilo del bot y el hilo de Flask
bot_config = {
    "username": None,
    "password": None,
    "room": None,
    "is_running": False,
    "status": "Not Started",
    "masters": ["y"] 
}

bot_state = {
    "SESSION_TOKEN": None,
    "user_id_cache": {},
    "banned_users": set(),
    "is_wc_on": False,
    "room_personalities": {},
    "groq_client": None,
    "websocket": None,
    "event_loop": None,
    "ping_task": None,
    "receive_task": None
}

# --- CONSTANTES Y URLS ---
SOCKET_URL = "wss://chatp.net:5333/server"
FILE_UPLOAD_URL = "https://cdn.talkinchat.com/post.php"
PROFILE_API_URL = "https://api.chatp.net/v2/user_profile"
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# --- LÓGICA DEL BOT (CLASES Y FUNCIONES) ---

class HoroScope:
    ZODIAC_SIGNS = {
        "aries": 1, "taurus": 2, "gemini": 3, "cancer": 4, "leo": 5, "virgo": 6,
        "libra": 7, "scorpio": 8, "sagittarius": 9, "capricorn": 10, "aquarius": 11, "pisces": 12
    }

    @staticmethod
    def get_horoscope(zodiac_sign: str, day: str) -> str:
        sign_number = HoroScope.ZODIAC_SIGNS.get(zodiac_sign.lower())
        if not sign_number: return "Galat rashi. Inme se chunein: " + ", ".join(HoroScope.ZODIAC_SIGNS.keys())
        if day.lower() not in ["yesterday", "today", "tomorrow"]: return "Galat din. 'yesterday', 'today', ya 'tomorrow' use karein."
        url = f"https://www.horoscope.com/us/horoscopes/general/horoscope-general-daily-{day.lower()}.aspx?sign={sign_number}"
        try:
            soup = BeautifulSoup(requests.get(url).content, "html.parser")
            return soup.find("div", class_="main-horoscope").p.text
        except Exception as e: return f"Rashifal laane mein dikkat hui: {e}"

COLOR_LIST = ["#F0F8FF","#FAEBD7","#0000FF","#8A2BE2","#A52A2A","#DEB887","#5F9EA0","#7FFF00","#D2691E","#FF7F50","#6495ED","#DC143C","#00FFFF","#00008B","#B8860B","#A9A9A9","#006400","#BDB76B","#8B008B","#556B2F","#FF8C00","#9932CC","#8B0000","#E9967A","#8FBC8F","#483D8B","#2F4F4F","#00CED1","#9400D3","#FF1493","#00BFFF","#696969","#1E90FF","#B22222","#228B22","#FF00FF","#DCDCDC","#FFD700","#DAA520","#808080","#008000","#ADFF2F","#FF69B4","#CD5C5C","#4B0082","#F0E68C","#E6E6FA","#7CFC00","#FFFACD","#ADD8E6","#F08080","#E0FFFF","#FAFAD2","#D3D3D3","#90EE90","#FFB6C1","#FFA07A","#20B2AA","#87CEFA","#778899","#B0C4DE","#FFFFE0","#00FF00","#32CD32","#FF00FF","#800000","#66CDAA","#0000CD","#BA55D3","#9370DB","#3CB371","#7B68EE","#00FA9A","#48D1CC","#C71585","#191970","#FFE4E1","#FFE4B5","#FFDEAD","#000080","#808000","#6B8E23","#FFA500","#FF4500","#DA70D6","#EEE8AA","#98FB98","#AFEEEE","#DB7093","#FFEFD5","#FFC0CB","#DDA0DD","#B0E0E6","#800080","#663399","#BC8F8F","#4169E1","#8B4513","#FA8072","#F4A460","#2E8B57","#A0522D","#C0C0C0","#87CEEB","#6A5ACD","#708090","#00FF7F","#4682B4","#D2B48C","#008080","#D8BFD8","#FF6347","#40E0D0","#EE82EE","#F5DEB3","#FFFFFF","#F5F5F5","#FFFF00","#9ACD32"]
IMG_TXT_FONTS = 'fonts/Merienda-Regular.ttf'
HANDLER, ID, TYPE, NAME, ROOM, MSG_BODY, MSG_FROM, USERNAME, PASSWORD = "handler", "id", "type", "name", "room", "body", "from", "username", "password"
HANDLER_LOGIN, HANDLER_LOGIN_EVENT, HANDLER_ROOM_JOIN, HANDLER_ROOM_MESSAGE, HANDLER_ROOM_EVENT = "login", "login_event", "room_join", "room_message", "room_event"
MSG_TYPE_TXT, MSG_TYPE_IMG, MSG_URL = "text", "image", "url"

CORE_PROMPT = """
[RULE: Your replies must be short, under 15 words.]
[IDENTITY: Your name is '{bot_name}'. You are not an AI; you are a chat companion.]
[LANGUAGE: Reply in the same language the user uses (English, Hindi, Hinglish, or Saudi Arabic/Arabi). Use the Saudi dialect for Arabic.]
"""
PERSONAS = {
    "sweet": f"{CORE_PROMPT}\n[MOOD: SWEET FRIEND]\nYou are positive and cheerful. Use cute emojis like ✨, 💖, 😊, 🥰.",
    "tsundere": f"{CORE_PROMPT}\n[MOOD: TSUNDERE]\nYou are harsh, but secretly care. Act annoyed. Use emojis like 😒, 🙄, 😠.",
}
DEFAULT_PERSONA = "sweet"

def generate_random_id(length=20): return ''.join(random.choice("0123456789abcdefghijklmnopqrstuvwxyz") for _ in range(length))

def search_bing_images(query):
    try:
        search_url = f"https://www.bing.com/images/search?q={quote_plus(query)}"
        response = requests.get(search_url, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(response.text, 'html.parser')
        for item in soup.find_all("a", class_="iusc"):
            if 'm' in item.attrs:
                mad_json = json.loads(item['m'])
                if 'murl' in mad_json and mad_json['murl']: return mad_json['murl']
    except Exception as e: print(f"[!] Image search error: {e}")
    return None

def draw_multiple_line_text(image, text, font, text_color, text_start_height):
    draw = ImageDraw.Draw(image)
    image_width, _ = image.size
    y_text = text_start_height
    lines = textwrap.wrap(text, width=25)
    for line in lines:
        line_width, line_height = draw.textbbox((0, 0), line, font=font)[2:]
        draw.text(((image_width - line_width) / 2, y_text), line, font=font, fill=text_color)
        y_text += line_height

def upload_image_php(file_path, room_name):
    try:
        multipart_data = MultipartEncoder(fields={
            'file': ('image.png', open(file_path, 'rb'), 'image/png'),
            'jid': bot_config["username"], 'is_private': 'no', 'room': room_name, 'device_id': generate_random_id(16)
        })
        response = requests.post(FILE_UPLOAD_URL, data=multipart_data, headers={'Content-Type': multipart_data.content_type})
        return response.text
    except Exception as e: print(f"[!] Image upload error: {e}"); return None

# --- FUNCIONES ASÍNCRONAS DEL BOT ---
async def get_ai_response_and_send(ws, room, sender, prompt):
    if not bot_state["groq_client"]: return await send_message(ws, room, MSG_TYPE_TXT, body="[!] AI feature configure nahi hai.")
    persona_template = PERSONAS[bot_state["room_personalities"].get(room, DEFAULT_PERSONA)]
    formatted_persona = persona_template.format(bot_name=bot_config["username"])
    try:
        completion = bot_state["groq_client"].chat.completions.create(
            messages=[{"role": "system", "content": formatted_persona}, {"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant", max_tokens=100
        )
        await send_message(ws, room, MSG_TYPE_TXT, body=f"{sender}\n{completion.choices[0].message.content}")
    except Exception as e: print(f"[!] Groq API Error: {e}")

async def get_user_profile(user_id):
    if not bot_state["SESSION_TOKEN"]: return None
    try:
        headers = {'Authorization': f'Bearer {bot_state["SESSION_TOKEN"]}', 'User-Agent': f'{bot_config["username"]}/1.0'}
        response = requests.get(PROFILE_API_URL, headers=headers, params={'user_id': user_id})
        return response.json() if response.status_code == 200 else None
    except Exception as e: print(f"[!] Profile fetch error: {e}"); return None

async def bot_login(ws):
    login_payload = {HANDLER: HANDLER_LOGIN, ID: generate_random_id(), USERNAME: bot_config["username"], PASSWORD: bot_config["password"]}
    print(f"[DEBUG] Sending login: username={bot_config['username']}")
    await ws.send(json.dumps(login_payload))

async def join_room(ws, room_name): await ws.send(json.dumps({HANDLER: HANDLER_ROOM_JOIN, ID: generate_random_id(), NAME: room_name}))
async def send_message(ws, room_name, msg_type, url="", body="", length=""): await ws.send(json.dumps({HANDLER: HANDLER_ROOM_MESSAGE, ID: generate_random_id(), ROOM: room_name, TYPE: msg_type, MSG_URL: url, "body": body, "length": length}))

async def handle_message(ws, data):
    try:
        sender, message, room = data.get(MSG_FROM), data.get(MSG_BODY, "").strip(), data.get(ROOM)
        if sender == bot_config["username"] or not message or sender in bot_state["banned_users"]: return

        if 'user_id' in data and sender: bot_state["user_id_cache"][sender.lower()] = data['user_id']
        
        triggers = [bot_config["username"].lower()]
        if any(trigger in message.lower() for trigger in triggers) and not message.startswith('!'):
            prompt = re.sub('|'.join(triggers), '', message, flags=re.IGNORECASE).strip(" @,:")
            if prompt: await get_ai_response_and_send(ws, room, sender, prompt)
            return

        if message.startswith("!"):
            parts = message.split(' ', 1)
            command, args = parts[0], (parts[1].strip() if len(parts) > 1 else "")
            
            # (El resto de tu lógica de comandos va aquí, sin cambios)
            if command == "!ai" and args: await get_ai_response_and_send(ws, room, sender, args)
            elif command == "!img" and args:
                await send_message(ws, room, MSG_TYPE_TXT, body=f"🖼️ '{args}' ki image dhoond raha hoon...")
                img_url = search_bing_images(args)
                if img_url: await send_message(ws, room, MSG_TYPE_IMG, url=img_url)
                else: await send_message(ws, room, MSG_TYPE_TXT, body=f"Sorry, '{args}' ki image nahi mili.")
            elif command == "!horo":
                horo_parts = args.split()
                if len(horo_parts) == 2:
                    horoscope_text = HoroScope.get_horoscope(horo_parts[0], horo_parts[1])
                    await send_message(ws, room, MSG_TYPE_TXT, body=f"**Horoscope for {horo_parts[0].capitalize()} ({horo_parts[1].capitalize()})**:\n{horoscope_text}")
                else: await send_message(ws, room, MSG_TYPE_TXT, body="Usage: !horo <rashi> <din>")
            # Añade el resto de tus comandos aquí...

    except Exception as e: print(f"[!] Command handle error: {e}")

async def on_user_joined(ws, data):
    user, room = data.get(USERNAME), data.get(NAME)
    print(f"[*] {user} ne {room} join kiya.")
    if bot_state["is_wc_on"]:
        # Lógica para crear y enviar tarjeta de bienvenida
        pass

async def send_pings(ws):
    while bot_config["is_running"]:
        try:
            await asyncio.sleep(25)
            if not bot_config["is_running"]: break
            await ws.send(json.dumps({"handler": "ping", "id": generate_random_id()}))
        except (websockets.exceptions.ConnectionClosed, Exception) as e: print(f"[!] Ping error: {e}"); break

async def receive_messages(websocket):
    try:
        async for payload in websocket:
            if not bot_config["is_running"]: break
            try:
                data = json.loads(payload)
                handler, event_type = data.get(HANDLER), data.get(TYPE)

                if handler == HANDLER_LOGIN_EVENT and event_type == "success":
                    if 's' in data: bot_state["SESSION_TOKEN"] = data['s']; print("[***] SESSION TOKEN MIL GAYA ***")
                    print(f"[+] Login successful! '{bot_config['room']}' join kar raha hoon...")
                    await join_room(websocket, bot_config["room"])
                
                elif event_type == "you_joined":
                    print(f"[*] Room join kar liya: {data.get('name')}")
                    await send_message(websocket, data.get('name'), MSG_TYPE_TXT, body=f"{bot_config['username']} is online! ✨")
                
                elif handler == HANDLER_ROOM_MESSAGE and event_type == MSG_TYPE_TXT: await handle_message(websocket, data)
                elif handler == HANDLER_ROOM_EVENT and event_type == "user_joined": await on_user_joined(websocket, data)

            except Exception as e: print(f"[!] Payload process error: {e}")
    except (websockets.exceptions.ConnectionClosed, Exception) as e: print(f"[!] Receive loop ended: {e}")

async def start_bot_main_loop():
    bot_config["status"] = "Connecting..."
    print(f"--- Bot '{bot_config['username']}' shuru ho raha hai ---")
    if GROQ_API_KEY: bot_state["groq_client"] = Groq(api_key=GROQ_API_KEY); print("[+] Groq AI initialized!")
    else: print("[!] GROQ_API_KEY not found. AI features disabled.")
    
    while bot_config["is_running"]:
        try:
            async with websockets.connect(SOCKET_URL, ssl=True) as websocket:
                bot_state["websocket"] = websocket
                print("[+] Server se connect ho gaya!")
                bot_config["status"] = "Connected"
                await bot_login(websocket)

                ping_task = asyncio.create_task(send_pings(websocket))
                receive_task = asyncio.create_task(receive_messages(websocket))
                bot_state["ping_task"], bot_state["receive_task"] = ping_task, receive_task

                done, pending = await asyncio.wait([ping_task, receive_task], return_when=asyncio.FIRST_COMPLETED)
                for task in pending: task.cancel()
        except Exception as e:
            print(f"[!] Bot loop error: {e}")
        
        if bot_config["is_running"]:
            bot_config["status"] = "Reconnecting..."
            print("[!] Reconectando en 10 segundos...")
            await asyncio.sleep(10)
    
    bot_config["status"] = "Stopped"
    print("[+] Bot stopped.")

def run_bot_async():
    """Esta función se ejecuta en un hilo separado."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_state["event_loop"] = loop
    try:
        loop.run_until_complete(start_bot_main_loop())
    finally:
        loop.close()
        bot_state["event_loop"] = None

async def shutdown_bot_tasks():
    """Corrutina para detener las tareas del bot de forma segura."""
    if bot_state["ping_task"]: bot_state["ping_task"].cancel()
    if bot_state["receive_task"]: bot_state["receive_task"].cancel()
    if bot_state["websocket"]: await bot_state["websocket"].close()
    print("[+] Tareas del bot canceladas.")

# --- PLANTILLAS HTML ---

LOGIN_HTML = '''
<!DOCTYPE html>
<html>
<head><title>Bot Login</title></head>
<body>
    <h1>🤖 Bot Control Panel</h1>
    <p>Enter bot credentials to start</p>
    {% if error %}<p style="color:red;">{{ error }}</p>{% endif %}
    <form method="POST">
        <input type="text" name="username" placeholder="Bot Username" required><br><br>
        <input type="password" name="password" placeholder="Bot Password" required><br><br>
        <input type="text" name="room" placeholder="Room Name" required><br><br>
        <button type="submit">Start Bot</button>
    </form>
</body>
</html>
'''

DASHBOARD_HTML = '''
<!DOCTYPE html>
<html>
<head><title>Bot Dashboard</title></head>
<body>
    <h1>🤖 Bot Control Panel</h1>
    <h2>Status: {{ status }}</h2>
    <p><strong>Bot:</strong> {{ username }}</p>
    <p><strong>Room:</strong> {{ room }}</p>
    
    <form method="POST" action="{{ url_for('stop_bot') }}" style="display:inline;">
        <button type="submit" {% if not is_running %}disabled{% endif %}>Stop Bot</button>
    </form>
    <a href="{{ url_for('logout') }}">Logout & Reconfigure</a>
    
    <h2>Available Commands</h2>
    <p>
        !ai &lt;message&gt;<br>
        !img &lt;query&gt;<br>
        !horo &lt;sign&gt; &lt;day&gt;<br>
        ... y otros comandos ...
    </p>
</body>
</html>
'''

# --- RUTAS DE FLASK ---

@app.route('/', methods=['GET', 'POST'])
def login():
    if 'logged_in' in session and bot_config['is_running']:
        return redirect(url_for('dashboard'))
    
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        room = request.form.get('room')
        
        if username and password and room:
            bot_config.update({
                "username": username,
                "password": password,
                "room": room,
                "is_running": True
            })
            
            # Iniciar el bot en un hilo separado para no bloquear la app web
            thread = threading.Thread(target=run_bot_async, daemon=True)
            thread.start()
            
            session['logged_in'] = True
            time.sleep(2) # Dar tiempo al bot para que se conecte
            return redirect(url_for('dashboard'))
        else:
            error = "All fields are required!"
    
    return render_template_string(LOGIN_HTML, error=error)

@app.route('/dashboard')
def dashboard():
    if 'logged_in' not in session or not bot_config['is_running']:
        return redirect(url_for('login'))
    
    return render_template_string(DASHBOARD_HTML, **bot_config)

@app.route('/stop-bot', methods=['POST'])
def stop_bot():
    if 'logged_in' in session:
        bot_config["is_running"] = False
        # Detener las tareas de forma segura desde el hilo principal de Flask
        if bot_state["event_loop"] and bot_state["event_loop"].is_running():
            future = asyncio.run_coroutine_threadsafe(shutdown_bot_tasks(), bot_state["event_loop"])
            future.result(timeout=5) # Esperar a que se complete
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    if bot_config["is_running"]:
        bot_config["is_running"] = False
        if bot_state["event_loop"] and bot_state["event_loop"].is_running():
            future = asyncio.run_coroutine_threadsafe(shutdown_bot_tasks(), bot_state["event_loop"])
            try:
                future.result(timeout=5)
            except Exception as e:
                print(f"Error durante el logout: {e}")
    
    session.clear()
    # Resetear la configuración para un nuevo login
    bot_config.update({
        "username": None, "password": None, "room": None, "status": "Not Started"
    })
    return redirect(url_for('login'))

# --- PUNTO DE ENTRADA ---
if __name__ == '__main__':
    # Esto es solo para pruebas locales. Render usará Gunicorn a través del Procfile.
    if not os.path.exists('fonts'):
        os.makedirs('fonts')
        print("[INFO] 'fonts' folder created. Please add .ttf font file.")
    app.run(host='0.0.0.0', port=5000, debug=False)
