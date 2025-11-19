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
import yt_dlp as youtube_dl
from dataclasses import dataclass

# ===============================================================
# --- SETTINGS & CONSTANTS ---
# ===============================================================
SOCKET_URL = "wss://chatp.net:5333/server"
FILE_UPLOAD_URL = "https://cdn.talkinchat.com/post.php"
PROFILE_API_URL = "https://api.chatp.net/v2/user_profile"
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
IMG_TXT_FONTS = 'fonts/Merienda-Regular.ttf'

# Constants
HANDLER = "handler"
TYPE = "type"
MSG_BODY = "body"
MSG_FROM = "from"
ROOM = "room"
HANDLER_LOGIN = "login"
HANDLER_ROOM_JOIN = "room_join"
HANDLER_ROOM_MESSAGE = "room_message"
HANDLER_ROOM_EVENT = "room_event"
MSG_TYPE_TXT = "text"
MSG_TYPE_IMG = "image"
MSG_TYPE_AUDIO = "audio"

COLOR_LIST = ["#F0F8FF","#FAEBD7","#0000FF","#8A2BE2","#A52A2A","#DEB887","#5F9EA0","#7FFF00","#D2691E","#FF7F50","#6495ED","#DC143C","#00FFFF","#00008B","#B8860B","#A9A9A9","#006400","#BDB76B","#8B008B","#556B2F","#FF8C00","#9932CC","#8B0000","#E9967A","#8FBC8F","#483D8B","#2F4F4F","#00CED1","#9400D3","#FF1493","#00BFFF","#696969","#1E90FF","#B22222","#228B22","#FF00FF","#DCDCDC","#FFD700","#DAA520","#808080","#008000","#ADFF2F","#FF69B4","#CD5C5C","#4B0082","#F0E68C","#E6E6FA","#7CFC00","#FFFACD","#ADD8E6","#F08080","#E0FFFF","#FAFAD2","#D3D3D3","#90EE90","#FFB6C1","#FFA07A","#20B2AA","#87CEFA","#778899","#B0C4DE","#FFFFE0","#00FF00","#32CD32","#FF00FF","#800000","#66CDAA","#0000CD","#BA55D3","#9370DB","#3CB371","#7B68EE","#00FA9A","#48D1CC","#C71585","#191970","#FFE4E1","#FFE4B5","#FFDEAD","#000080","#800000","#6B8E23","#FFA500","#FF4500","#DA70D6","#EEE8AA","#98FB98","#AFEEEE","#DB7093","#FFEFD5","#FFC0CB","#DDA0DD","#B0E0E6","#800080","#663399","#BC8F8F","#4169E1","#8B4513","#FA8072","#F4A460","#2E8B57","#A0522D","#C0C0C0","#87CEEB","#6A5ACD","#708090","#00FF7F","#4682B4","#D2B48C","#008080","#D8BFD8","#FF6347","#40E0D0","#EE82EE","#F5DEB3","#FFFFFF","#F5F5F5","#FFFF00","#9ACD32"]

app = Flask(__name__)
app.secret_key = os.getenv("SESSION_SECRET") or secrets.token_hex(32)

# --- GLOBAL STATE ---
bot_config = {
    "username": "", "password": "", "room": "",
    "is_running": False, "status": "Stopped", "masters": ["y"]
}
bot_state = {
    "SESSION_TOKEN": None, "websocket": None, "loop": None,
    "is_wc_on": False, "user_id_cache": {}, "groq_client": None,
    "room_personalities": {}
}

# ===============================================================
# --- HELPER CLASSES ---
# ===============================================================

@dataclass
class Song:
    url: str = ""
    duration: int = 0
    thumb_url: str = ""
    title: str = "Unknown Title"

class HoroScope:
    ZODIAC_SIGNS = {
        "aries": 1, "taurus": 2, "gemini": 3, "cancer": 4, "leo": 5, "virgo": 6,
        "libra": 7, "scorpio": 8, "sagittarius": 9, "capricorn": 10, "aquarius": 11, "pisces": 12
    }
    @staticmethod
    def get_horoscope(zodiac_sign: str, day: str) -> str:
        sign_number = HoroScope.ZODIAC_SIGNS.get(zodiac_sign.lower())
        if not sign_number: return "Galat rashi."
        url = f"https://www.horoscope.com/us/horoscopes/general/horoscope-general-daily-{day.lower()}.aspx?sign={sign_number}"
        try:
            soup = BeautifulSoup(requests.get(url).content, "html.parser")
            return soup.find("div", class_="main-horoscope").p.text
        except Exception as e: return f"Rashifal Error: {e}"

CORE_PROMPT = """
[RULE: Reply shortly under 15 words.]
[IDENTITY: You are '{bot_name}'. Not an AI, but a chat companion.]
[LANGUAGE: Reply in user's language (English/Hindi/Arabic).]
"""
PERSONAS = {
    "sweet": f"{CORE_PROMPT}\n[MOOD: SWEET]\nBe cute & use emojis ✨.",
    "tsundere": f"{CORE_PROMPT}\n[MOOD: TSUNDERE]\nAct annoyed but care 😒.",
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
            'jid': bot_config["username"], 'room': room_name, 'device_id': generate_random_id(16)
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

def scrape_music_from_yt(searchQuery):
    ydl_opts = {'format': 'm4a/bestaudio/best', 'noplaylist': True, 'quiet': True}
    try:
        with youtube_dl.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch:{searchQuery}", download=False)['entries'][0]
            return Song(url=info['url'], duration=info.get('duration', 0), thumb_url=info['thumbnail'], title=info.get('title', 'Unknown'))
    except: return None

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
    if not bot_state["SESSION_TOKEN"]: return None
    try:
        headers = {'Authorization': f'Bearer {bot_state["SESSION_TOKEN"]}', 'User-Agent': 'IyadBot/1.0'}
        res = requests.get(PROFILE_API_URL, headers=headers, params={'user_id': user_id})
        return res.json() if res.status_code == 200 else None
    except: return None

# ===============================================================
# --- CORE LOGIC ---
# ===============================================================

async def send_packet(ws, data):
    await ws.send(json.dumps(data))

async def send_message(ws, room, msg_type, body="", url="", length="0"):
    packet = {
        HANDLER: HANDLER_ROOM_MESSAGE, "id": generate_random_id(), ROOM: room,
        TYPE: msg_type, "body": body, "msg_url": url, "length": str(length)
    }
    await send_packet(ws, packet)

async def get_ai_reply(ws, room, sender, prompt):
    if not bot_state["groq_client"]: return await send_message(ws, room, MSG_TYPE_TXT, body="[!] AI Key Missing.")
    
    p_temp = PERSONAS.get(bot_state["room_personalities"].get(room, DEFAULT_PERSONA), DEFAULT_PERSONA)
    final_persona = p_temp.format(bot_name=bot_config["username"])
    
    try:
        completion = bot_state["groq_client"].chat.completions.create(
            messages=[{"role": "system", "content": final_persona}, {"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant", max_tokens=100
        )
        await send_message(ws, room, MSG_TYPE_TXT, body=f"@{sender} {completion.choices[0].message.content}")
    except Exception as e: print(f"AI Error: {e}")

async def handle_incoming_message(ws, data):
    try:
        sender = data.get(MSG_FROM)
        message = data.get(MSG_BODY, "").strip()
        room = data.get(ROOM)

        if sender == bot_config["username"] or not message: return
        if 'user_id' in data: bot_state["user_id_cache"][sender.lower()] = data['user_id']

        # --- SINGLE TRIGGER LOGIC ---
        # Ab sirf username hi trigger hai (Not "Iyad" etc.)
        trigger_name = bot_config["username"].lower()
        
        if trigger_name in message.lower() and not message.startswith("!"):
            prompt = message.lower().replace(trigger_name, "", 1).strip(" @,:")
            # Original case prompt chahiye AI ke liye
            original_prompt = re.sub(re.escape(bot_config["username"]), "", message, flags=re.IGNORECASE).strip(" @,:")
            
            if original_prompt: 
                print(f"[TRIGGERED] by {sender}: {original_prompt}")
                await get_ai_reply(ws, room, sender, original_prompt)
            return

        # --- COMMANDS ---
        if message.startswith("!"):
            parts = message.split(' ', 1)
            cmd = parts[0].lower()
            args = parts[1].strip() if len(parts) > 1 else ""

            # 1. AI
            if cmd == "!ai" and args: await get_ai_reply(ws, room, sender, args)
            
            # 2. MUSIC
            elif cmd == "!play" and args:
                await send_message(ws, room, MSG_TYPE_TXT, body=f"🎶 Searching '{args}'...")
                song = scrape_music_from_yt(args)
                if song and song.url:
                    if song.thumb_url: await send_message(ws, room, MSG_TYPE_IMG, url=song.thumb_url)
                    await send_message(ws, room, MSG_TYPE_AUDIO, url=song.url, length=song.duration)
                else: await send_message(ws, room, MSG_TYPE_TXT, body="Song nahi mila.")

            # 3. IMAGE
            elif cmd == "!img" and args:
                await send_message(ws, room, MSG_TYPE_TXT, body=f"🖼️ Finding {args}...")
                link = search_bing_images(args)
                if link: await send_message(ws, room, MSG_TYPE_IMG, url=link)
                else: await send_message(ws, room, MSG_TYPE_TXT, body="Image not found.")

            # 4. HOROSCOPE
            elif cmd == "!horo":
                parts = args.split()
                if len(parts) == 2:
                    res = HoroScope.get_horoscope(parts[0], parts[1])
                    await send_message(ws, room, MSG_TYPE_TXT, body=f"🔮 {parts[0]}:\n{res}")
                else: await send_message(ws, room, MSG_TYPE_TXT, body="Use: !horo <rashi> <day>")

            # 5. PROFILE
            elif cmd == "!profile":
                target = args.lstrip('@').lower() if args else sender.lower()
                uid = bot_state["user_id_cache"].get(target)
                if uid:
                    p = await get_user_profile(uid)
                    if p: await send_message(ws, room, MSG_TYPE_TXT, body=f"👤 Name: {p.get('name')}\n🆔 ID: {uid}\n📍 Loc: {p.get('location')}")
                else: await send_message(ws, room, MSG_TYPE_TXT, body="User info not cached yet.")

            # 6. DRAW
            elif cmd == "!draw" and 'avatar_url' in data:
                try:
                    try: font = ImageFont.truetype(IMG_TXT_FONTS, 60)
                    except: font = ImageFont.load_default()
                    resp = requests.get(data['avatar_url'])
                    img = Image.open(BytesIO(resp.content)).resize((800,800)).filter(ImageFilter.GaussianBlur(15))
                    draw_multiple_line_text(img, args, font, random.choice(COLOR_LIST), 300)
                    img.save('draw.png')
                    link = upload_image_php('draw.png', room)
                    if link: await send_message(ws, room, MSG_TYPE_IMG, url=link)
                except Exception as e: print(f"Draw Fail: {e}")

            # 7. MASTER COMMANDS
            if sender in bot_config["masters"] or sender == bot_config["username"]:
                if cmd == "!wc":
                    bot_state["is_wc_on"] = not bot_state["is_wc_on"]
                    await send_message(ws, room, MSG_TYPE_TXT, body=f"Welcome Card: {bot_state['is_wc_on']}")
                elif cmd == "!persona" and args in PERSONAS:
                    bot_state["room_personalities"][room] = args
                    await send_message(ws, room, MSG_TYPE_TXT, body=f"Persona set to {args}")
                elif cmd == "!join" and args:
                    await send_packet(ws, {HANDLER: HANDLER_ROOM_JOIN, "id": generate_random_id(), "name": args})

    except Exception as e: print(f"Handler Error: {e}")

async def on_user_joined(ws, data):
    if bot_state["is_wc_on"]:
        try:
            u, r = data.get("username"), data.get("name")
            img = Image.new('RGB', (800,600), random.choice(COLOR_LIST))
            font = ImageFont.truetype(IMG_TXT_FONTS, 60)
            draw_multiple_line_text(img, f"Welcome\n{u}", font, "#000000", 200)
            img.save('wc.png')
            link = upload_image_php('wc.png', r)
            if link: await send_message(ws, r, MSG_TYPE_IMG, url=link)
        except Exception as e: print(f"WC Error: {e}")

# --- SOCKET ENGINE ---
async def bot_engine():
    if GROQ_API_KEY: bot_state["groq_client"] = Groq(api_key=GROQ_API_KEY)
    
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    while bot_config["is_running"]:
        try:
            print(f"[*] Connecting to {SOCKET_URL}...")
            async with websockets.connect(SOCKET_URL, ssl=ssl_ctx) as ws:
                bot_state["websocket"] = ws
                bot_config["status"] = "Connected"
                print("[+] Connected!")

                await send_packet(ws, {
                    HANDLER: HANDLER_LOGIN, "id": generate_random_id(),
                    "username": bot_config["username"], "password": bot_config["password"]
                })

                async def pinger():
                    while bot_config["is_running"]:
                        await asyncio.sleep(25)
                        try: await send_packet(ws, {"handler": "ping", "id": generate_random_id()})
                        except: break
                asyncio.create_task(pinger())

                async for raw_msg in ws:
                    if not bot_config["is_running"]: break
                    try:
                        data = json.loads(raw_msg)
                        h = data.get(HANDLER)
                        t = data.get(TYPE)

                        if h == "login_event" and t == "success":
                            bot_state["SESSION_TOKEN"] = data.get('s')
                            print("[+] Login OK. Joining Room...")
                            await send_packet(ws, {HANDLER: HANDLER_ROOM_JOIN, "id": generate_random_id(), "name": bot_config["room"]})

                        elif t == "you_joined":
                            print(f"[+] Joined Room: {data.get('name')}")
                            await send_message(ws, data.get('name'), MSG_TYPE_TXT, body="Online! 🚀")

                        elif h == HANDLER_ROOM_MESSAGE and t == MSG_TYPE_TXT:
                            await handle_incoming_message(ws, data)

                        elif h == HANDLER_ROOM_EVENT and t == "user_joined":
                            await on_user_joined(ws, data)

                    except Exception as e: print(f"Parse Error: {e}")

        except Exception as e:
            print(f"Connection Error: {e}")
            bot_config["status"] = "Error"
            await asyncio.sleep(5)

# --- FLASK ---
def start_background_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_state["loop"] = loop
    loop.run_until_complete(bot_engine())

@app.route("/", methods=["GET", "POST"])
def index():
    if 'logged_in' in session and bot_config['is_running']: return redirect('/dashboard')
    if request.method == "POST":
        bot_config["username"] = request.form.get("username")
        bot_config["password"] = request.form.get("password")
        bot_config["room"] = request.form.get("room")
        bot_config["is_running"] = True
        if "y" not in bot_config["masters"]: bot_config["masters"].append("y")
        bot_config["masters"].append(bot_config["username"])
        
        t = threading.Thread(target=start_background_thread)
        t.daemon = True
        t.start()
        
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

# --- TEMPLATES ---
LOGIN_HTML = """
<!DOCTYPE html><html><body style='font-family:sans-serif;text-align:center;margin-top:50px'>
<h2>🤖 Web Bot Login</h2>
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
<p>!play [song], !ai [msg], !img [query], !horo [sign] [day], !draw [text], !profile, !wc</p>
</body></html>
"""

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
