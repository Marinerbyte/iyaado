import asyncio
import json
import random
import threading
import websockets
import requests
import os
import secrets
import ssl
import re
import time
from urllib.parse import quote_plus
from io import BytesIO
from flask import Flask, render_template_string, request, redirect, url_for, session
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from requests_toolbelt.multipart.encoder import MultipartEncoder
from bs4 import BeautifulSoup
from groq import Groq
import textwrap

# ===============================================================
# --- CONFIGURATIONS ---
# ===============================================================
SOCKET_URL = "wss://chatp.net:5333/server"
FILE_UPLOAD_URL = "https://cdn.talkinchat.com/post.php"
PROFILE_API_URL = "https://api.chatp.net/v2/user_profile"
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
IMG_TXT_FONTS = 'fonts/Merienda-Regular.ttf'

# --- CONSTANTS (Protocol Samjha Hua) ---
HANDLER = "handler"
TYPE = "type"
MSG_BODY = "body"
MSG_FROM = "from"
ROOM = "room"
NAME = "name"
ID = "id"
USERNAME = "username"
PASSWORD = "password"

# Server Protocol (Learned from tanvar.py)
# Server sends chat messages inside 'room_event' with type 'text'
HANDLER_LOGIN = "login"
HANDLER_LOGIN_EVENT = "login_event"
HANDLER_ROOM_JOIN = "room_join"
HANDLER_ROOM_EVENT = "room_event"     # <--- Server sends chats here
HANDLER_ROOM_MESSAGE = "room_message" # <--- We send chats here
MSG_TYPE_TXT = "text"
MSG_TYPE_IMG = "image"
EVENT_TYPE_SUCCESS = "success"

COLOR_LIST = ["#F0F8FF","#FAEBD7","#0000FF","#8A2BE2","#FFD700","#DC143C","#00FFFF"]

app = Flask(__name__)
app.secret_key = os.getenv("SESSION_SECRET") or secrets.token_hex(32)

# --- GLOBAL STATE ---
bot_config = {
    "username": "", "password": "", "room": "",
    "is_running": False, "status": "Stopped", "masters": ["y"]
}
bot_state = {
    "websocket": None, "loop": None,
    "is_wc_on": False, "groq_client": None, "thread": None,
    "room_personalities": {}, "user_id_cache": {}, "session_token": None
}

# --- PERSONAS ---
CORE_PROMPT = """
[RULE: Reply shortly under 20 words.]
[IDENTITY: You are '{bot_name}'. Not an AI, but a chat companion.]
[LANGUAGE: Reply in user's language (English/Hindi/Arabic).]
"""
PERSONAS = {
    "sweet": f"{CORE_PROMPT}\n[MOOD: SWEET]\nBe cute & use emojis ✨.",
    "tsundere": f"{CORE_PROMPT}\n[MOOD: TSUNDERE]\nAct annoyed but care 😒.",
    "rude": f"{CORE_PROMPT}\n[MOOD: RUDE]\nBe sarcastic and mean.",
}
DEFAULT_PERSONA = "sweet"

# ===============================================================
# --- HELPER FUNCTIONS ---
# ===============================================================

def generate_random_id(length=20):
    return ''.join(random.choice("0123456789abcdefghijklmnopqrstuvwxyz") for _ in range(length))

def check_and_download_font():
    if not os.path.exists('fonts'): os.makedirs('fonts')
    if not os.path.exists(IMG_TXT_FONTS):
        try:
            url = "https://github.com/google/fonts/raw/main/ofl/merienda/Merienda-Regular.ttf"
            with open(IMG_TXT_FONTS, 'wb') as f: f.write(requests.get(url).content)
        except: pass
check_and_download_font()

def upload_image_php(file_path, room_name):
    try:
        data = MultipartEncoder(fields={
            'file': ('image.png', open(file_path, 'rb'), 'image/png'),
            'jid': bot_config["username"], 'is_private': 'no', 'room': room_name, 'device_id': generate_random_id(16)
        })
        headers = {'Content-Type': data.content_type, 'User-Agent': 'okhttp/3.12.1'}
        return requests.post(FILE_UPLOAD_URL, data=data, headers=headers).text
    except: return None

def search_bing_images(query):
    try:
        url = f"https://www.bing.com/images/search?q={quote_plus(query)}"
        soup = BeautifulSoup(requests.get(url, headers={"User-Agent": "Mozilla/5.0"}).text, 'html.parser')
        for item in soup.find_all("a", class_="iusc"):
            if 'm' in item.attrs:
                m = json.loads(item['m'])
                if 'murl' in m: return m['murl']
    except: pass
    return None

def draw_multiple_line_text(image, text, font, text_color, text_start_height):
    draw = ImageDraw.Draw(image)
    w, _ = image.size
    y = text_start_height
    lines = textwrap.wrap(text, width=25)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        lw, lh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((w - lw) / 2, y), line, font=font, fill=text_color)
        y += lh + 5

async def get_user_profile(user_id):
    if not bot_state["session_token"]: return None
    try:
        headers = {'Authorization': f'Bearer {bot_state["session_token"]}', 'User-Agent': 'IyadBot/1.0'}
        res = requests.get(PROFILE_API_URL, headers=headers, params={'user_id': user_id})
        return res.json() if res.status_code == 200 else None
    except: return None

class HoroScope:
    ZODIAC_SIGNS = { "aries": 1, "taurus": 2, "gemini": 3, "cancer": 4, "leo": 5, "virgo": 6, "libra": 7, "scorpio": 8, "sagittarius": 9, "capricorn": 10, "aquarius": 11, "pisces": 12 }
    @staticmethod
    def get_horoscope(zodiac_sign: str, day: str) -> str:
        sign_number = HoroScope.ZODIAC_SIGNS.get(zodiac_sign.lower())
        if not sign_number: return "Invalid Sign."
        url = f"https://www.horoscope.com/us/horoscopes/general/horoscope-general-daily-{day.lower()}.aspx?sign={sign_number}"
        try:
            soup = BeautifulSoup(requests.get(url).content, "html.parser")
            return soup.find("div", class_="main-horoscope").p.text
        except: return "Error fetching horoscope."

# ===============================================================
# --- BOT LOGIC ---
# ===============================================================

async def send_packet(ws, data):
    await ws.send(json.dumps(data))

async def send_group_msg(ws, room, msg):
    # Sending logic: We use 'room_message' (Standard)
    jsonbody = {HANDLER: HANDLER_ROOM_MESSAGE, ID: generate_random_id(), ROOM: room, TYPE: MSG_TYPE_TXT, "url": "", MSG_BODY: msg, "length": "0"}
    await ws.send(json.dumps(jsonbody))

async def send_group_msg_image(ws, room, url):
    jsonbody = {HANDLER: HANDLER_ROOM_MESSAGE, ID: generate_random_id(), ROOM: room, TYPE: MSG_TYPE_IMG, "url": url, MSG_BODY: "", "length": "0"}
    await ws.send(json.dumps(jsonbody))

async def get_ai_reply(ws, room, sender, prompt):
    if not bot_state["groq_client"]: return await send_group_msg(ws, room, "[!] AI Not Configured.")
    
    p_key = bot_state["room_personalities"].get(room, DEFAULT_PERSONA)
    p_temp = PERSONAS.get(p_key, DEFAULT_PERSONA)
    final_persona = p_temp.format(bot_name=bot_config["username"])

    try:
        completion = bot_state["groq_client"].chat.completions.create(
            messages=[{"role": "system", "content": final_persona}, {"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant", max_tokens=100
        )
        await send_group_msg(ws, room, f"@{sender} {completion.choices[0].message.content}")
    except Exception as e: print(f"AI Error: {e}")

async def on_message(ws, data):
    msg = data.get(MSG_BODY, "")
    frm = data.get(MSG_FROM)
    room = data.get(ROOM)
    user_avi = data.get('avatar_url')

    if frm == bot_config["username"]: return
    if 'user_id' in data: bot_state["user_id_cache"][frm.lower()] = data['user_id']

    # --- 1. TRIGGER LOGIC (Only on Username) ---
    trigger_name = bot_config["username"].lower()
    if trigger_name in msg.lower() and not msg.startswith("!"):
        prompt = msg.lower().replace(trigger_name, "", 1).strip(" @,:")
        if prompt: 
            print(f"[TRIGGER] {frm}: {prompt}")
            await get_ai_reply(ws, room, frm, prompt)
        return

    # --- 2. COMMANDS (Only ID.PY features) ---
    if msg.startswith("!"):
        try:
            parts = msg.split(' ', 1)
            cmd = parts[0].lower()
            args = parts[1].strip() if len(parts) > 1 else ""

            # AI Manual
            if cmd == "!ai" and args: await get_ai_reply(ws, room, frm, args)
            
            # Persona Change
            elif cmd == "!persona" and args:
                if args.lower() in PERSONAS:
                    bot_state["room_personalities"][room] = args.lower()
                    await send_group_msg(ws, room, f"Mode set to: {args}")
                else:
                    await send_group_msg(ws, room, f"Available: {', '.join(PERSONAS.keys())}")

            # Welcome Toggle (Master Only)
            elif cmd == "!wc" and (frm in bot_config["masters"] or frm == bot_config["username"]):
                bot_state["is_wc_on"] = not bot_state["is_wc_on"]
                await send_group_msg(ws, room, f"Welcome Card: {bot_state['is_wc_on']}")

            # Image
            elif cmd == "!img" and args:
                await send_group_msg(ws, room, "Searching image...")
                link = search_bing_images(args)
                if link: await send_group_msg_image(ws, room, link)
                else: await send_group_msg(ws, room, "No image found.")

            # Profile
            elif cmd == "!profile":
                target = args.lstrip('@').lower() if args else frm.lower()
                uid = bot_state["user_id_cache"].get(target)
                if uid:
                    p = await get_user_profile(uid)
                    if p: await send_group_msg(ws, room, f"👤 {p.get('name')} | 🆔 {uid}")
                else: await send_group_msg(ws, room, "User not seen yet.")
            
            # Horoscope
            elif cmd == "!horo" and args:
                p = args.split()
                if len(p) == 2: await send_group_msg(ws, room, HoroScope.get_horoscope(p[0], p[1]))

            # Draw
            elif cmd == "!draw" and user_avi:
                font = ImageFont.truetype(IMG_TXT_FONTS, 60)
                response = requests.get(user_avi)
                avatar = Image.open(BytesIO(response.content)).resize((800,800)).filter(ImageFilter.GaussianBlur(15))
                draw_multiple_line_text(avatar, args, font, random.choice(COLOR_LIST), 300)
                avatar.save('pil_text.png')
                link = upload_image_php('pil_text.png', room)
                if link: await send_group_msg_image(ws, room, link)

        except Exception as e: print(f"Cmd Error: {e}")

async def on_wc_draw(ws, data):
    if not bot_state["is_wc_on"]: return
    try:
        user = data.get(USERNAME)
        room = data.get(NAME)
        img = Image.new('RGB', (800, 600), color=random.choice(COLOR_LIST))
        img.filter(ImageFilter.GaussianBlur(40))
        font = ImageFont.truetype(IMG_TXT_FONTS, 60)
        text1 = f"Welcome to {room}\n{user}"
        draw_multiple_line_text(img, text1, font, random.choice(COLOR_LIST), 150)
        img.save('pil_text.png')
        link = upload_image_php('pil_text.png', room)
        if link: await send_group_msg_image(ws, room, link)
    except: pass

# --- MAIN SOCKET ENGINE ---
async def bot_engine():
    if GROQ_API_KEY: bot_state["groq_client"] = Groq(api_key=GROQ_API_KEY)
    
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    while bot_config["is_running"]:
        try:
            print(f"[*] Connecting to {SOCKET_URL}...")
            async with websockets.connect(SOCKET_URL, ssl=ssl_ctx) as ws:
                print("[+] Connected!")

                # LOGIN
                await send_packet(ws, {
                    HANDLER: HANDLER_LOGIN, ID: generate_random_id(),
                    USERNAME: bot_config["username"], PASSWORD: bot_config["password"]
                })

                # PING LOOP
                async def pinger():
                    while bot_config["is_running"]:
                        await asyncio.sleep(15)
                        try: await send_packet(ws, {"handler": "ping", "id": generate_random_id()})
                        except: break
                asyncio.create_task(pinger())

                async for raw_msg in ws:
                    if not bot_config["is_running"]: break
                    try:
                        data = json.loads(raw_msg)
                        handler = data.get(HANDLER)
                        evt_type = data.get(TYPE)

                        # Login Success
                        if handler == HANDLER_LOGIN_EVENT and evt_type == EVENT_TYPE_SUCCESS:
                            bot_state["session_token"] = data.get('s')
                            print("[+] Logged In. Joining Room...")
                            await send_packet(ws, {HANDLER: HANDLER_ROOM_JOIN, ID: generate_random_id(), NAME: bot_config["room"]})

                        # === PROTOCOL FIX ===
                        # Server sends chat messages as 'room_event' with type 'text' (Learned from tanvar.py)
                        elif handler == HANDLER_ROOM_EVENT and evt_type == MSG_TYPE_TXT:
                            await on_message(ws, data)
                        
                        # Fallback for standard message just in case
                        elif handler == HANDLER_ROOM_MESSAGE and evt_type == MSG_TYPE_TXT:
                            await on_message(ws, data)

                        # User Joined (Welcome)
                        elif handler == HANDLER_ROOM_EVENT and evt_type == "user_joined":
                            await on_wc_draw(ws, data)

                    except Exception as e: print(f"Parse Error: {e}")

        except Exception as e:
            print(f"Connection Error: {e}")
            await asyncio.sleep(5)

# --- FLASK ---
def start_background_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_state["loop"] = loop
    loop.run_until_complete(bot_engine())

@app.route("/", methods=["GET", "POST"])
def index():
    if bot_config["is_running"]: return redirect('/dashboard')
    if request.method == "POST":
        bot_config["username"] = request.form.get("username")
        bot_config["password"] = request.form.get("password")
        bot_config["room"] = request.form.get("room")
        bot_config["is_running"] = True
        
        if bot_config["username"] not in bot_config["masters"]:
            bot_config["masters"].append(bot_config["username"])

        if bot_state["thread"] is None or not bot_state["thread"].is_alive():
            t = threading.Thread(target=start_background_thread)
            t.daemon = True
            t.start()
            bot_state["thread"] = t
        
        session['logged_in'] = True
        return redirect("/dashboard")
    return render_template_string(LOGIN_HTML)

@app.route("/dashboard")
def dashboard():
    return render_template_string(DASHBOARD_HTML, **bot_config)

@app.route("/stop", methods=["POST"])
def stop():
    bot_config["is_running"] = False
    return redirect("/dashboard")

# Self Wake (Keep Alive)
def self_wake():
    while True:
        time.sleep(300)
        try: 
            if bot_config["is_running"]: requests.get("http://127.0.0.1:5000/")
        except: pass
threading.Thread(target=self_wake, daemon=True).start()

# Templates
LOGIN_HTML = """
<!DOCTYPE html><html><body style='font-family:sans-serif;text-align:center;margin-top:50px'>
<h2>🤖 Enhanced ID Bot</h2>
<form method='POST' style='max-width:300px;margin:auto'>
<input name='username' placeholder='Username' required style='width:100%;padding:10px;margin:5px'><br>
<input name='password' placeholder='Password' type='password' required style='width:100%;padding:10px;margin:5px'><br>
<input name='room' placeholder='Room Name' required style='width:100%;padding:10px;margin:5px'><br>
<button style='width:100%;padding:10px;background:blue;color:white;border:none'>START BOT</button>
</form></body></html>
"""

DASHBOARD_HTML = """
<!DOCTYPE html><html><body style='font-family:sans-serif;text-align:center;margin-top:50px'>
<h1>Status: {{ status }}</h1>
<p>Bot: <b>{{ username }}</b> | Room: <b>{{ room }}</b></p>
<form action='/stop' method='POST'><button style='background:red;color:white;padding:10px'>STOP BOT</button></form>
<hr>
<h3>Trigger:</h3>
<p>Only <b>{{ username }}</b></p>
<h3>Commands:</h3>
<p>!ai, !persona [mode], !img, !horo, !draw, !profile, !wc</p>
</body></html>
"""

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
