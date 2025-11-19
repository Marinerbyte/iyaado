import asyncio
import json
import random
import threading
import websockets
import requests
import os
import secrets
import ssl
import time
import aiohttp
from urllib.parse import quote_plus
from io import BytesIO
from flask import Flask, render_template_string, request, redirect, url_for, session, flash
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from bs4 import BeautifulSoup
from groq import AsyncGroq
import textwrap

# ===============================================================
# --- CONFIGURATIONS ---
# ===============================================================
SOCKET_URL = "wss://chatp.net:5333/server"
FILE_UPLOAD_URL = "https://cdn.talkinchat.com/post.php"
PROFILE_API_URL = "https://api.chatp.net/v2/user_profile"
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
IMG_TXT_FONTS = 'fonts/Merienda-Regular.ttf'
PORT = int(os.environ.get('PORT', 5000))

# --- CONSTANTS ---
HANDLER = "handler"
TYPE = "type"
MSG_BODY = "body"
MSG_FROM = "from"
ROOM = "room"
NAME = "name"
ID = "id"
USERNAME = "username"
PASSWORD = "password"
MSG_TYPE_TXT = "text"
MSG_TYPE_IMG = "image"
COLOR_LIST = ["#F0F8FF","#FAEBD7","#0000FF","#8A2BE2","#FFD700","#DC143C","#00FFFF"]

app = Flask(__name__)
app.secret_key = os.getenv("SESSION_SECRET") or secrets.token_hex(32)

# --- GLOBAL STORE FOR ACTIVE BOTS ---
# Format: { "username_lower": BotInstanceObject }
ACTIVE_BOTS = {}

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
            requests.get(url)
            with open(IMG_TXT_FONTS, 'wb') as f: f.write(requests.get(url).content)
        except: pass
check_and_download_font()

# --- ASYNC HELPERS ---
async def async_upload_image(file_bytes, room_name, username):
    try:
        form = aiohttp.FormData()
        form.add_field('file', file_bytes, filename='image.png', content_type='image/png')
        form.add_field('jid', username)
        form.add_field('is_private', 'no')
        form.add_field('room', room_name)
        form.add_field('device_id', generate_random_id(16))
        async with aiohttp.ClientSession() as session:
            async with session.post(FILE_UPLOAD_URL, data=form) as resp:
                return await resp.text()
    except: return None

async def async_search_bing(query):
    try:
        url = f"https://www.bing.com/images/search?q={quote_plus(query)}"
        headers = {"User-Agent": "Mozilla/5.0"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                html = await resp.text()
        soup = BeautifulSoup(html, 'html.parser')
        for item in soup.find_all("a", class_="iusc"):
            if 'm' in item.attrs:
                m = json.loads(item['m'])
                if 'murl' in m: return m['murl']
    except: pass
    return None

# --- PROCESSORS (Thread Safe) ---
def process_draw_image(avatar_bytes, text):
    try:
        font = ImageFont.truetype(IMG_TXT_FONTS, 60)
        avatar = Image.open(BytesIO(avatar_bytes)).resize((800,800)).filter(ImageFilter.GaussianBlur(15))
        draw = ImageDraw.Draw(avatar)
        w, _ = avatar.size
        y = 300
        lines = textwrap.wrap(text, width=25)
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            lw, lh = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text(((w - lw) / 2, y), line, font=font, fill=random.choice(COLOR_LIST))
            y += lh + 5
        out = BytesIO()
        avatar.save(out, format='PNG')
        return out.getvalue()
    except: return None

def process_wc_image(user, room):
    try:
        img = Image.new('RGB', (800, 600), color=random.choice(COLOR_LIST))
        img.filter(ImageFilter.GaussianBlur(40))
        font = ImageFont.truetype(IMG_TXT_FONTS, 60)
        draw = ImageDraw.Draw(img)
        text = f"Welcome to {room}\n{user}"
        w, h = img.size
        y = 150
        for line in text.split('\n'):
            bbox = draw.textbbox((0, 0), line, font=font)
            lw = bbox[2] - bbox[0]
            draw.text(((w - lw) / 2, y), line, font=font, fill=random.choice(COLOR_LIST))
            y += 100
        out = BytesIO()
        img.save(out, format='PNG')
        return out.getvalue()
    except: return None

def process_horoscope(sign, day):
    zodiac_signs = { "aries": 1, "taurus": 2, "gemini": 3, "cancer": 4, "leo": 5, "virgo": 6, "libra": 7, "scorpio": 8, "sagittarius": 9, "capricorn": 10, "aquarius": 11, "pisces": 12 }
    sign_number = zodiac_signs.get(sign.lower())
    if not sign_number: return "Invalid Sign."
    try:
        url = f"https://www.horoscope.com/us/horoscopes/general/horoscope-general-daily-{day.lower()}.aspx?sign={sign_number}"
        soup = BeautifulSoup(requests.get(url).content, "html.parser")
        return soup.find("div", class_="main-horoscope").p.text
    except: return "Error fetching horoscope."

# ===============================================================
# --- THE BOT CLASS (MULTI USER LOGIC) ---
# ===============================================================

class TalkinChatBot:
    def __init__(self, username, password, room):
        self.username = username
        self.password = password
        self.room = room
        self.is_running = True
        self.status = "Running"
        self.masters = [username.lower(), "y"]
        
        # State
        self.loop = None
        self.groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
        self.room_personalities = {}
        self.user_id_cache = {}
        self.session_token = None
        self.is_wc_on = False

    async def get_user_profile(self, user_id):
        if not self.session_token: return None
        try:
            headers = {'Authorization': f'Bearer {self.session_token}', 'User-Agent': 'IyadBot/1.0'}
            async with aiohttp.ClientSession() as session:
                async with session.get(PROFILE_API_URL, headers=headers, params={'user_id': user_id}) as resp:
                    return await resp.json() if resp.status == 200 else None
        except: return None

    async def send_packet(self, ws, data):
        try: await ws.send(json.dumps(data))
        except: pass

    async def send_msg(self, ws, room, msg, type=MSG_TYPE_TXT, url=""):
        body = {HANDLER: "room_message", ID: generate_random_id(), ROOM: room, TYPE: type, "url": url, MSG_BODY: msg, "length": "0"}
        await self.send_packet(ws, body)

    async def get_ai_reply(self, ws, room, sender, prompt):
        if not self.groq_client: return await self.send_msg(ws, room, "[!] AI Not Configured.")
        p_key = self.room_personalities.get(room, DEFAULT_PERSONA)
        final_persona = PERSONAS.get(p_key, DEFAULT_PERSONA).format(bot_name=self.username)
        try:
            completion = await self.groq_client.chat.completions.create(
                messages=[{"role": "system", "content": final_persona}, {"role": "user", "content": prompt}],
                model="llama-3.1-8b-instant", max_tokens=100
            )
            await self.send_msg(ws, room, f"#{sender} {completion.choices[0].message.content}")
        except Exception as e: print(f"AI Error: {e}")

    async def engine(self):
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        
        while self.is_running:
            try:
                print(f"[*] {self.username} connecting...")
                async with websockets.connect(SOCKET_URL, ssl=ssl_ctx) as ws:
                    # Login
                    await self.send_packet(ws, {HANDLER: "login", ID: generate_random_id(), USERNAME: self.username, PASSWORD: self.password})
                    
                    # Pinger
                    async def pinger():
                        while self.is_running:
                            await asyncio.sleep(15)
                            try: await self.send_packet(ws, {"handler": "ping", "id": generate_random_id()})
                            except: break
                    asyncio.create_task(pinger())

                    async for raw in ws:
                        if not self.is_running: break
                        try:
                            data = json.loads(raw)
                            h = data.get(HANDLER)
                            t = data.get(TYPE)
                            
                            if h == "login_event" and t == "success":
                                self.session_token = data.get('s')
                                print(f"[+] {self.username} Logged In. Joining {self.room}...")
                                await self.send_packet(ws, {HANDLER: "room_join", ID: generate_random_id(), NAME: self.room})

                            elif h == "room_event" and t == "user_joined":
                                if self.is_wc_on:
                                    loop = asyncio.get_running_loop()
                                    img = await loop.run_in_executor(None, process_wc_image, data.get(USERNAME), data.get(NAME))
                                    if img:
                                        lnk = await async_upload_image(img, data.get(NAME), self.username)
                                        if lnk: await self.send_msg(ws, data.get(NAME), "", MSG_TYPE_IMG, lnk)

                            elif (h == "room_event" or h == "room_message") and t == MSG_TYPE_TXT:
                                await self.handle_message(ws, data)
                        except: pass
            except Exception as e:
                print(f"Err {self.username}: {e}")
                await asyncio.sleep(5)
        print(f"[-] {self.username} Stopped.")

    async def handle_message(self, ws, data):
        msg = data.get(MSG_BODY, "")
        frm = data.get(MSG_FROM)
        room = data.get(ROOM)
        user_avi = data.get('avatar_url')
        if frm == self.username: return
        if 'user_id' in data: self.user_id_cache[frm.lower()] = data['user_id']

        # AI Trigger
        trigger = self.username.lower()
        if trigger in msg.lower() and not msg.startswith("!"):
            prompt = msg.lower().replace(trigger, "", 1).strip(" @,:")
            if prompt: asyncio.create_task(self.get_ai_reply(ws, room, frm, prompt))
            return

        # Commands
        if msg.startswith("!"):
            loop = asyncio.get_running_loop()
            try:
                parts = msg.split(' ', 1)
                cmd = parts[0].lower()
                args = parts[1].strip() if len(parts) > 1 else ""

                if cmd == "!ai" and args: asyncio.create_task(self.get_ai_reply(ws, room, frm, args))
                elif cmd == "!persona" and args:
                    if args.lower() in PERSONAS:
                        self.room_personalities[room] = args.lower()
                        await self.send_msg(ws, room, f"#{frm} Mode: {args}")
                elif cmd == "!wc" and (frm in self.masters):
                    self.is_wc_on = not self.is_wc_on
                    await self.send_msg(ws, room, f"#{frm} WC: {self.is_wc_on}")
                elif cmd == "!img" and args:
                    await self.send_msg(ws, room, "🔎")
                    lnk = await async_search_bing(args)
                    if lnk: await self.send_msg(ws, room, "", MSG_TYPE_IMG, lnk)
                    else: await self.send_msg(ws, room, "Not found")
                elif cmd == "!profile":
                    target = args.lstrip('@').lower() if args else frm.lower()
                    uid = self.user_id_cache.get(target)
                    if uid:
                        p = await self.get_user_profile(uid)
                        if p: await self.send_msg(ws, room, f"👤 {p.get('name')} | 🆔 {uid}")
                    else: await self.send_msg(ws, room, "User hidden/unknown")
                elif cmd == "!horo" and args:
                    p = args.split()
                    day = p[1] if len(p) > 1 else "today"
                    res = await loop.run_in_executor(None, process_horoscope, p[0], day)
                    await self.send_msg(ws, room, f"#{frm} {res}")
                elif cmd == "!draw" and user_avi and args:
                    await self.send_msg(ws, room, "🎨")
                    async with aiohttp.ClientSession() as session:
                        async with session.get(user_avi) as resp:
                            avi_bytes = await resp.read()
                    img = await loop.run_in_executor(None, process_draw_image, avi_bytes, args)
                    if img:
                        lnk = await async_upload_image(img, room, self.username)
                        if lnk: await self.send_msg(ws, room, "", MSG_TYPE_IMG, lnk)
            except: pass

    def run_thread(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        loop.run_until_complete(self.engine())

# ===============================================================
# --- FLASK ROUTES ---
# ===============================================================

@app.route("/", methods=["GET", "POST"])
def index():
    if 'username' in session:
        return redirect('/dashboard')
        
    if request.method == "POST":
        user = request.form.get("username")
        pwd = request.form.get("password")
        room = request.form.get("room")
        
        # Create Bot Instance
        user_key = user.lower()
        if user_key in ACTIVE_BOTS:
            flash("Bot already running! Logged in.")
        else:
            new_bot = TalkinChatBot(user, pwd, room)
            t = threading.Thread(target=new_bot.run_thread)
            t.daemon = True
            t.start()
            ACTIVE_BOTS[user_key] = new_bot
            
        session['username'] = user_key
        return redirect("/dashboard")
    return render_template_string(LOGIN_HTML)

@app.route("/dashboard")
def dashboard():
    user_key = session.get('username')
    if not user_key or user_key not in ACTIVE_BOTS:
        session.pop('username', None)
        return redirect("/")
    
    bot = ACTIVE_BOTS[user_key]
    return render_template_string(DASHBOARD_HTML, 
                                  username=bot.username, 
                                  room=bot.room, 
                                  status="Running" if bot.is_running else "Stopped",
                                  wc_status="ON" if bot.is_wc_on else "OFF")

@app.route("/stop", methods=["POST"])
def stop():
    user_key = session.get('username')
    if user_key and user_key in ACTIVE_BOTS:
        bot = ACTIVE_BOTS[user_key]
        bot.is_running = False
        del ACTIVE_BOTS[user_key]
        session.pop('username', None)
    return redirect("/")

@app.route("/logout")
def logout():
    session.pop('username', None)
    return redirect("/")

# Self Wake (Keeps Render Alive)
def self_wake():
    while True:
        time.sleep(300)
        try: requests.get(f"http://127.0.0.1:{PORT}/")
        except: pass
threading.Thread(target=self_wake, daemon=True).start()

# HTML TEMPLATES
LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head><title>MultiBot Panel</title><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style='font-family:sans-serif;text-align:center;background:#f0f2f5;padding-top:50px'>
<div style='background:white;max-width:350px;margin:auto;padding:20px;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,0.1)'>
<h2>🤖 Bot Manager</h2>
{% with messages = get_flashed_messages() %}
  {% if messages %}<p style="color:red">{{ messages[0] }}</p>{% endif %}
{% endwith %}
<form method='POST'>
<input name='username' placeholder='Bot Username' required style='width:90%;padding:12px;margin:8px;border:1px solid #ddd;border-radius:5px'><br>
<input name='password' placeholder='Password' type='password' required style='width:90%;padding:12px;margin:8px;border:1px solid #ddd;border-radius:5px'><br>
<input name='room' placeholder='Room Name' required style='width:90%;padding:12px;margin:8px;border:1px solid #ddd;border-radius:5px'><br>
<button style='width:100%;padding:12px;background:#0084ff;color:white;border:none;border-radius:5px;font-weight:bold;cursor:pointer'>LAUNCH BOT 🚀</button>
</form>
</div>
</body></html>
"""

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head><title>Dashboard</title><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style='font-family:sans-serif;text-align:center;background:#f0f2f5;padding-top:50px'>
<div style='background:white;max-width:350px;margin:auto;padding:20px;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,0.1)'>
<h1>👋 Hi, {{ username }}</h1>
<div style='text-align:left;background:#eee;padding:15px;border-radius:5px;margin-bottom:20px'>
<p><b>Status:</b> {{ status }} 🟢</p>
<p><b>Room:</b> {{ room }}</p>
<p><b>Welcome Card:</b> {{ wc_status }}</p>
</div>
<h3>Commands:</h3>
<p style='font-size:12px;color:#555'>!ai [msg], !img [query], !draw [text], !horo [sign], !wc, !profile</p>
<hr>
<form action='/stop' method='POST'>
<button style='width:100%;padding:12px;background:#ff4d4d;color:white;border:none;border-radius:5px;cursor:pointer;margin-bottom:10px'>🛑 STOP BOT</button>
</form>
<a href='/logout' style='color:blue;text-decoration:none'>Log out (Keep Bot Running)</a>
</div>
</body></html>
"""

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT)
