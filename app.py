import asyncio
import json
import random
import time
import threading
import websockets
import requests
import urllib
from urllib.parse import quote_plus
from io import BytesIO
import os
import re
import secrets
from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from requests_toolbelt.multipart.encoder import MultipartEncoder
from bs4 import BeautifulSoup
from groq import Groq
import textwrap

app = Flask(__name__)
app.secret_key = os.getenv("SESSION_SECRET") or secrets.token_hex(32)

SOCKET_URL = "wss://chatp.net:5333/server"
FILE_UPLOAD_URL = "https://cdn.talkinchat.com/post.php"
PROFILE_API_URL = "https://api.chatp.net/v2/user_profile"
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

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
    "room_state": {"users": {}, "subject": "Unknown", "name": ""},
    "banned_users": set(),
    "is_wc_on": False,
    "room_personalities": {},
    "groq_client": None,
    "websocket": None,
    "event_loop": None,
    "ping_task": None,
    "receive_task": None
}

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
MSG_TYPE_TXT, MSG_TYPE_IMG, MSG_TYPE_AUDIO, MSG_URL, MSG_LENGTH = "text", "image", "audio", "url", "length"

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

def generate_random_id(length=20): 
    return ''.join(random.choice("0123456789abcdefghijklmnopqrstuvwxyz") for _ in range(length))

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
        try: line_width, line_height = font.getsize(line)
        except AttributeError: line_width, line_height = draw.textsize(line, font=font)
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
    except Exception as e:
        print(f"[!] Image upload error: {e}")
        return None

async def get_ai_response_and_send(ws, room, sender, prompt):
    if not bot_state["groq_client"]: 
        return await send_message(ws, room, MSG_TYPE_TXT, body="[!] AI feature configure nahi hai.")
    
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

async def join_room(ws, room_name): 
    await ws.send(json.dumps({HANDLER: HANDLER_ROOM_JOIN, ID: generate_random_id(), NAME: room_name}))

async def send_message(ws, room_name, msg_type, url="", body="", length=""): 
    await ws.send(json.dumps({HANDLER: HANDLER_ROOM_MESSAGE, ID: generate_random_id(), ROOM: room_name, TYPE: msg_type, MSG_URL: url, MSG_BODY: body, MSG_LENGTH: length}))

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
            command_body = message.split(' ', 1)
            command = command_body[0]
            args = command_body[1].strip() if len(command_body) > 1 else ""

            if command == "!ai":
                if args: await get_ai_response_and_send(ws, room, sender, args)
            
            elif command == "!img":
                if not args: return
                await send_message(ws, room, MSG_TYPE_TXT, body=f"🖼️ '{args}' ki image dhoond raha hoon...")
                img_url = search_bing_images(args)
                if img_url: await send_message(ws, room, MSG_TYPE_IMG, url=img_url)
                else: await send_message(ws, room, MSG_TYPE_TXT, body=f"Sorry, '{args}' ki image nahi mili.")
            
            elif command == "!profile":
                target_user = args.lstrip('@').lower()
                if target_user not in bot_state["user_id_cache"]: 
                    return await send_message(ws, room, MSG_TYPE_TXT, body=f"Sorry, {target_user} ki info nahi hai.")
                user_id = bot_state["user_id_cache"][target_user]
                await send_message(ws, room, MSG_TYPE_TXT, body=f"🔍 @{target_user} (ID: {user_id}) ki jaankari nikal raha hoon...")
                profile_data = await get_user_profile(user_id)
                if profile_data:
                    response_body = f"--- @{target_user} Profile ---\n" + json.dumps(profile_data, indent=2, ensure_ascii=False)
                    await send_message(ws, room, MSG_TYPE_TXT, body=response_body)
                else: await send_message(ws, room, MSG_TYPE_TXT, body=f"Sorry, {target_user} ki profile nahi mil paayi.")

            elif command == "!horo":
                parts = args.split()
                if len(parts) == 2:
                    horoscope_text = HoroScope.get_horoscope(parts[0], parts[1])
                    await send_message(ws, room, MSG_TYPE_TXT, body=f"**Horoscope for {parts[0].capitalize()} ({parts[1].capitalize()})**:\n{horoscope_text}")
                else: await send_message(ws, room, MSG_TYPE_TXT, body="Usage: !horo <rashi> <din>")

            elif command == "!draw":
                if not args or 'avatar_url' not in data or not data['avatar_url']: return
                try:
                    response = requests.get(data['avatar_url'])
                    avatar = Image.open(BytesIO(response.content)).resize((800, 800)).filter(ImageFilter.GaussianBlur(radius=15))
                    font = ImageFont.truetype(IMG_TXT_FONTS, 60)
                    draw_multiple_line_text(avatar, args, font, random.choice(COLOR_LIST), 300)
                    avatar.save('temp_draw.png')
                    link = upload_image_php('temp_draw.png', room)
                    if link: await send_message(ws, room, MSG_TYPE_IMG, url=link)
                except Exception as e: print(f"[!] Draw command error: {e}")

            if sender.lower() in [m.lower() for m in bot_config["masters"]]:
                if command == "!addm":
                    if args:
                        new_master = args.strip().lstrip('@')
                        if new_master not in bot_config["masters"]:
                            bot_config["masters"].append(new_master)
                            await send_message(ws, room, MSG_TYPE_TXT, body=f"✅ {new_master} ko master bana diya gaya!")
                        else:
                            await send_message(ws, room, MSG_TYPE_TXT, body=f"⚠️ {new_master} pehle se master hai.")
                    else:
                        await send_message(ws, room, MSG_TYPE_TXT, body="Usage: !addm <username>")
                
                elif command == "!wc":
                    bot_state["is_wc_on"] = not bot_state["is_wc_on"]
                    await send_message(ws, room, MSG_TYPE_TXT, body=f"Welcome card ab {'ON' if bot_state['is_wc_on'] else 'OFF'} hai.")
                
                elif command == "!join":
                    if args: await join_room(ws, args)
                
                elif command == "!quit":
                    await ws.send(json.dumps({HANDLER: "room_leave", NAME: room, ID: generate_random_id()}))
                    await send_message(ws, room, MSG_TYPE_TXT, body="Theek hai, mai jaa raha hoon.")
                
                elif command == "!persona":
                    if args.lower() in PERSONAS:
                        bot_state["room_personalities"][room] = args.lower()
                        await send_message(ws, room, MSG_TYPE_TXT, body=f"Mera mood ab {args.capitalize()} hai. ✨")
                    else: await send_message(ws, room, MSG_TYPE_TXT, body=f"Sirf 'sweet' ya 'tsundere' persona set kar sakte hain.")

    except Exception as e:
        print(f"[!] Command handle error: {e}")

async def on_user_joined(ws, data):
    user, room = data.get(USERNAME), data.get(NAME)
    print(f"[*] {user} ne {room} join kiya.")
    if bot_state["is_wc_on"]:
        try:
            image = Image.new('RGB', (800, 600), color=random.choice(COLOR_LIST)).filter(ImageFilter.GaussianBlur(radius=20))
            font = ImageFont.truetype(IMG_TXT_FONTS, 60)
            draw_multiple_line_text(image, f"Welcome to {room}\n{user}", font, random.choice(COLOR_LIST), 150)
            image.save('welcome.png')
            link = upload_image_php('welcome.png', room)
            if link: await send_message(ws, room, MSG_TYPE_IMG, url=link)
        except Exception as e: print(f"[!] Welcome card error: {e}")

async def send_pings(ws):
    while bot_config["is_running"]:
        try:
            await asyncio.sleep(25)
            if not bot_config["is_running"]:
                break
            await ws.send(json.dumps({"handler": "ping", "id": generate_random_id()}))
        except (websockets.exceptions.ConnectionClosed, Exception) as e:
            print(f"[!] Ping error: {e}")
            break

async def receive_messages(websocket):
    try:
        async for payload in websocket:
            if not bot_config["is_running"]:
                break
            
            try:
                data = json.loads(payload)
                handler, event_type = data.get(HANDLER), data.get(TYPE)
                print(f"[DEBUG] Received: handler={handler}, type={event_type}")

                if handler == HANDLER_LOGIN_EVENT and event_type == "success":
                    if 's' in data:
                        bot_state["SESSION_TOKEN"] = data['s']
                        print(f"[***] SESSION TOKEN MIL GAYA: {bot_state['SESSION_TOKEN'][:10]}... [***]")
                    print(f"[+] Login successful! '{bot_config['room']}' join kar raha hoon...")
                    await join_room(websocket, bot_config["room"])
                
                elif event_type == "you_joined":
                    print(f"[*] Room join kar liya: {data.get('name')}")
                    bot_state["room_state"]["name"] = data.get("name")
                    for user in data.get("users", []): 
                        bot_state["room_state"]["users"][user["username"]] = {"role": user["role"]}
                    await send_message(websocket, bot_state['room_state']['name'], MSG_TYPE_TXT, 
                                     body=f"{bot_config['username']} is online! ✨ Commands: !ai, !img, !horo, !profile, !draw, !addm")
                
                elif handler == HANDLER_ROOM_EVENT:
                    if event_type == MSG_TYPE_TXT: await handle_message(websocket, data)
                    elif event_type == "user_joined": await on_user_joined(websocket, data)

            except Exception as e: 
                print(f"[!] Payload process error: {e}")
    except (websockets.exceptions.ConnectionClosed, Exception) as e:
        print(f"[!] Receive messages loop ended: {e}")

async def start_bot():
    bot_config["status"] = "Connecting..."
    print(f"--- Super Bot v2 '{bot_config['username']}' shuru ho raha hai ---")
    
    if GROQ_API_KEY:
        bot_state["groq_client"] = Groq(api_key=GROQ_API_KEY)
        print("[+] Groq AI initialized!")
    else:
        print("[!] GROQ_API_KEY not found. AI features disabled.")
    
    while bot_config["is_running"]:
        try:
            async with websockets.connect(SOCKET_URL, ssl=True) as websocket:
                bot_state["websocket"] = websocket
                print("[+] Server se connect ho gaya!")
                bot_config["status"] = "Connected"
                await bot_login(websocket)

                ping_task = asyncio.create_task(send_pings(websocket))
                receive_task = asyncio.create_task(receive_messages(websocket))
                bot_state["ping_task"] = ping_task
                bot_state["receive_task"] = receive_task

                done, pending = await asyncio.wait(
                    [ping_task, receive_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                
                bot_state["websocket"] = None
                bot_state["ping_task"] = None
                bot_state["receive_task"] = None

        except websockets.exceptions.ConnectionClosed as e:
            print(f"[!] Connection band ho gaya: {e}")
            bot_config["status"] = "Reconnecting..." if bot_config["is_running"] else "Stopped"
        except Exception as e:
            print(f"[!] Unexpected error: {e}")
            bot_config["status"] = "Error" if bot_config["is_running"] else "Stopped"
        
        if bot_config["is_running"]:
            await asyncio.sleep(10)
    
    bot_config["status"] = "Stopped"
    print("[+] Bot stopped.")

def run_bot_async():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_state["event_loop"] = loop
    try:
        loop.run_until_complete(start_bot())
    finally:
        bot_state["event_loop"] = None

LOGIN_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bot Control Panel - Login</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .login-container {
            background: white;
            padding: 40px;
            border-radius: 10px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            max-width: 400px;
            width: 100%;
        }
        h1 {
            color: #667eea;
            margin-bottom: 10px;
            text-align: center;
        }
        p {
            color: #666;
            margin-bottom: 30px;
            text-align: center;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            margin-bottom: 8px;
            color: #333;
            font-weight: 500;
        }
        input {
            width: 100%;
            padding: 12px;
            border: 2px solid #e1e8ed;
            border-radius: 5px;
            font-size: 14px;
            transition: border 0.3s;
        }
        input:focus {
            outline: none;
            border-color: #667eea;
        }
        button {
            width: 100%;
            padding: 12px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 5px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.2s;
        }
        button:hover {
            transform: translateY(-2px);
        }
        .error {
            background: #fee;
            color: #c33;
            padding: 10px;
            border-radius: 5px;
            margin-bottom: 20px;
            text-align: center;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <h1>🤖 Bot Control Panel</h1>
        <p>Enter bot credentials to start</p>
        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}
        <form method="POST">
            <div class="form-group">
                <label for="username">Bot Username</label>
                <input type="text" id="username" name="username" required placeholder="Enter bot username">
            </div>
            <div class="form-group">
                <label for="password">Bot Password</label>
                <input type="password" id="password" name="password" required placeholder="Enter bot password">
            </div>
            <div class="form-group">
                <label for="room">Room Name</label>
                <input type="text" id="room" name="room" required placeholder="Enter room name">
            </div>
            <button type="submit">Start Bot</button>
        </form>
    </div>
</body>
</html>
'''

DASHBOARD_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bot Control Panel - Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #f5f7fa;
            padding: 20px;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 10px;
            margin-bottom: 20px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.1);
        }
        .header h1 {
            margin-bottom: 10px;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        .card {
            background: white;
            padding: 25px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }
        .card h2 {
            color: #667eea;
            margin-bottom: 20px;
            border-bottom: 2px solid #e1e8ed;
            padding-bottom: 10px;
        }
        .status-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        .status-item {
            padding: 15px;
            background: #f8f9fa;
            border-radius: 5px;
            border-left: 4px solid #667eea;
        }
        .status-item label {
            display: block;
            color: #666;
            font-size: 12px;
            margin-bottom: 5px;
            text-transform: uppercase;
        }
        .status-item .value {
            font-size: 18px;
            font-weight: 600;
            color: #333;
        }
        .status-running {
            border-left-color: #52c41a;
        }
        .status-stopped {
            border-left-color: #f5222d;
        }
        .btn {
            padding: 12px 24px;
            border: none;
            border-radius: 5px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            margin-right: 10px;
            text-decoration: none;
            display: inline-block;
        }
        .btn-stop {
            background: #f5222d;
            color: white;
        }
        .btn-stop:hover {
            background: #cf1322;
        }
        .btn-logout {
            background: #666;
            color: white;
        }
        .btn-logout:hover {
            background: #444;
        }
        .master-list {
            list-style: none;
        }
        .master-item {
            padding: 12px;
            background: #f8f9fa;
            border-radius: 5px;
            margin-bottom: 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .master-item .name {
            font-weight: 600;
            color: #333;
        }
        .master-item .badge {
            background: #667eea;
            color: white;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
        }
        .btn-remove {
            background: #ff4d4f;
            color: white;
            padding: 6px 12px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
        }
        .btn-remove:hover {
            background: #cf1322;
        }
        .form-inline {
            display: flex;
            gap: 10px;
            margin-top: 15px;
        }
        .form-inline input {
            flex: 1;
            padding: 10px;
            border: 2px solid #e1e8ed;
            border-radius: 5px;
        }
        .btn-add {
            background: #52c41a;
            color: white;
            padding: 10px 20px;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-weight: 600;
        }
        .btn-add:hover {
            background: #389e0d;
        }
        .success-msg {
            background: #f6ffed;
            border: 1px solid #b7eb8f;
            color: #52c41a;
            padding: 12px;
            border-radius: 5px;
            margin-bottom: 15px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🤖 Bot Control Panel</h1>
            <p>Manage your chatbot with ease</p>
        </div>

        <div class="card">
            <h2>Bot Status</h2>
            <div class="status-grid">
                <div class="status-item {{ 'status-running' if is_running else 'status-stopped' }}">
                    <label>Status</label>
                    <div class="value">{{ status }}</div>
                </div>
                <div class="status-item">
                    <label>Bot Username</label>
                    <div class="value">{{ username }}</div>
                </div>
                <div class="status-item">
                    <label>Current Room</label>
                    <div class="value">{{ room }}</div>
                </div>
                <div class="status-item">
                    <label>Total Masters</label>
                    <div class="value">{{ masters|length }}</div>
                </div>
            </div>
            <div>
                <form method="POST" action="{{ url_for('stop_bot') }}" style="display:inline;">
                    <button type="submit" class="btn btn-stop" {% if not is_running %}disabled{% endif %}>Stop Bot</button>
                </form>
                <a href="{{ url_for('logout') }}" class="btn btn-logout">Logout & Reconfigure</a>
            </div>
        </div>

        <div class="card">
            <h2>Master Users</h2>
            {% if success %}
            <div class="success-msg">{{ success }}</div>
            {% endif %}
            <p style="margin-bottom: 15px; color: #666;">Master users can use admin commands like !addm, !join, !quit, etc.</p>
            <ul class="master-list">
                {% for master in masters %}
                <li class="master-item">
                    <span class="name">{{ master }}</span>
                    <div>
                        {% if master == 'y' %}
                        <span class="badge">Default Master</span>
                        {% else %}
                        <form method="POST" action="{{ url_for('remove_master') }}" style="display:inline;">
                            <input type="hidden" name="master" value="{{ master }}">
                            <button type="submit" class="btn-remove">Remove</button>
                        </form>
                        {% endif %}
                    </div>
                </li>
                {% endfor %}
            </ul>
            <form method="POST" action="{{ url_for('add_master') }}" class="form-inline">
                <input type="text" name="master" placeholder="Enter username to add as master" required>
                <button type="submit" class="btn-add">Add Master</button>
            </form>
        </div>

        <div class="card">
            <h2>Available Commands</h2>
            <p style="color: #666; line-height: 1.8;">
                <strong>!ai &lt;message&gt;</strong> - Chat with AI<br>
                <strong>!img &lt;query&gt;</strong> - Search and send images<br>
                <strong>!profile @username</strong> - Get user profile<br>
                <strong>!horo &lt;sign&gt; &lt;day&gt;</strong> - Get horoscope<br>
                <strong>!draw &lt;text&gt;</strong> - Draw text on avatar<br>
                <strong>!addm &lt;username&gt;</strong> - Add master (masters only)<br>
                <strong>!join &lt;room&gt;</strong> - Join another room (masters only)<br>
                <strong>!quit</strong> - Leave current room (masters only)<br>
                <strong>!persona &lt;sweet/tsundere&gt;</strong> - Change bot personality (masters only)<br>
                <strong>!wc</strong> - Toggle welcome card (masters only)
            </p>
        </div>
    </div>
</body>
</html>
'''

@app.route('/', methods=['GET', 'POST'])
def login():
    if 'logged_in' in session and session['logged_in']:
        return redirect(url_for('dashboard'))
    
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        room = request.form.get('room')
        
        if username and password and room:
            bot_config["username"] = username
            bot_config["password"] = password
            bot_config["room"] = room
            bot_config["is_running"] = True
            
            session['logged_in'] = True
            
            thread = threading.Thread(target=run_bot_async, daemon=True)
            thread.start()
            
            return redirect(url_for('dashboard'))
        else:
            error = "All fields are required!"
    
    return render_template_string(LOGIN_HTML, error=error)

@app.route('/dashboard')
def dashboard():
    if 'logged_in' not in session or not session['logged_in']:
        return redirect(url_for('login'))
    
    success = request.args.get('success')
    
    return render_template_string(DASHBOARD_HTML, 
                                 username=bot_config["username"],
                                 room=bot_config["room"],
                                 status=bot_config["status"],
                                 is_running=bot_config["is_running"],
                                 masters=bot_config["masters"],
                                 success=success)

@app.route('/add-master', methods=['POST'])
def add_master():
    if 'logged_in' not in session or not session['logged_in']:
        return redirect(url_for('login'))
    
    master = request.form.get('master', '').strip().lstrip('@')
    if master and master not in bot_config["masters"]:
        bot_config["masters"].append(master)
        return redirect(url_for('dashboard', success=f'{master} added as master!'))
    
    return redirect(url_for('dashboard'))

@app.route('/remove-master', methods=['POST'])
def remove_master():
    if 'logged_in' not in session or not session['logged_in']:
        return redirect(url_for('login'))
    
    master = request.form.get('master', '').strip()
    if master in bot_config["masters"] and master != 'y':
        bot_config["masters"].remove(master)
        return redirect(url_for('dashboard', success=f'{master} removed from masters!'))
    
    return redirect(url_for('dashboard'))

async def shutdown_bot():
    if bot_state["ping_task"] and not bot_state["ping_task"].done():
        bot_state["ping_task"].cancel()
    if bot_state["receive_task"] and not bot_state["receive_task"].done():
        bot_state["receive_task"].cancel()
    if bot_state["websocket"]:
        try:
            await bot_state["websocket"].close()
        except:
            pass

@app.route('/stop-bot', methods=['POST'])
def stop_bot():
    if 'logged_in' not in session or not session['logged_in']:
        return redirect(url_for('login'))
    
    bot_config["is_running"] = False
    if bot_state["event_loop"] and bot_state["event_loop"].is_running():
        asyncio.run_coroutine_threadsafe(shutdown_bot(), bot_state["event_loop"])
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    bot_config["is_running"] = False
    if bot_state["event_loop"] and bot_state["event_loop"].is_running():
        asyncio.run_coroutine_threadsafe(shutdown_bot(), bot_state["event_loop"])
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    if not os.path.exists('fonts'):
        os.makedirs('fonts')
        print("[INFO] 'fonts' folder created. Please add .ttf font file for draw commands.")
    
    app.run(host='0.0.0.0', port=5000, debug=False)
