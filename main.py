import os
import re
import requests
from flask import Flask, request, jsonify
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import asyncio
import json
import base64
import time
import io
from PIL import Image
import threading

app = Flask(__name__)

# ============================================
# CACHE SYSTEM
# TTL = 6 hours — same URL won't hit Nick Bot again within this window
# ============================================
CACHE_TTL = 6 * 60 * 60  # seconds
_cache = {}          # url → {"bypassed": str, "ts": float}
_cache_lock = threading.Lock()

def cache_get(url):
    with _cache_lock:
        entry = _cache.get(url)
        if entry and (time.time() - entry["ts"]) < CACHE_TTL:
            return entry["bypassed"]
        return None

def cache_set(url, bypassed):
    with _cache_lock:
        _cache[url] = {"bypassed": bypassed, "ts": time.time()}

def cache_stats():
    with _cache_lock:
        now = time.time()
        total = len(_cache)
        alive = sum(1 for e in _cache.values() if (now - e["ts"]) < CACHE_TTL)
        return {"total_entries": total, "alive_entries": alive, "ttl_hours": CACHE_TTL // 3600}

# ============================================
# HARDCODED CREDENTIALS
# ============================================
API_ID = 32128791
API_HASH = "b6274d1ac3319bffcab4f9a6015167c7"
_HARDCODED_SESSION = "1BVtsOMcBuxrvmHJl_oZPfTeQX3B-ywDy62AVG3CyobDw2gBJ8N2-QbqlXcDzhWIAK3YUhPn27xpZ6Y6Y1kF99_9CHNBmYaSgUvr4yyzv6Wrj0Yr33jETOCtJp-sMiRjkpVU0At5QSoYyzTpr1H-Z6m_YuoCbMMRFcR-ZpYBqun9t22UPSsWsphw4Bxus7w6Zxe4j2qPW3H1wDPmLuLtxRPpjQD5r5b8Q_IVFeaxg5u8V4RbxdsdcmRh_Oj4r5ifvzARVP1B1H5xGrNOGB7MoTKrbe5yLLImCtTGv4SPXZ-f5o2oy5E9VBZuDCjO2HyDAW1MFEAXPAyiB5OC2G73qcxOv4rt7tjk="
SESSION_STRING = os.environ.get("SESSION_STRING") or _HARDCODED_SESSION
BOT_USERNAME = "Nick_Bypass_Bot"

# ============================================
# GITHUB ACCESS TOKEN
# ============================================
GITHUB_TOKEN = "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"

GITHUB_API_URL = "https://models.inference.ai.azure.com/chat/completions"

# ============================================
# CAPTCHA SOLVE — GRID TYPE (@CaptchaAPI)
#
# The captcha is a colored grid (4x4).
# Each colored box has a small number in its corner.
# One box contains a ghost/icon image.
# Task: find which box has the icon → return its number.
# ============================================
CAPTCHA_PROMPT = (
    "This is a grid CAPTCHA image (from @CaptchaAPI). "
    "It shows a 4×4 grid of colored boxes (green and pink). "
    "Each box has a small number printed in one of its corners. "
    "Exactly ONE box contains a small icon or ghost image inside it — all other boxes are empty (just a number). "
    "Your task: identify the box that contains the icon/ghost image, "
    "then read the small number printed in that same box. "
    "Reply with ONLY that number — no words, no explanation, just the digits."
)

def solve_captcha_github_ai(image_data):
    """Try GPT-4o then Phi-3.5-vision to read the grid captcha number."""
    try:
        img_b64 = base64.b64encode(image_data).decode('utf-8')

        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Content-Type": "application/json"
        }

        models = ["gpt-4o", "Phi-3.5-vision-instruct"]

        for model_name in models:
            payload = {
                "model": model_name,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{img_b64}"
                                }
                            },
                            {
                                "type": "text",
                                "text": CAPTCHA_PROMPT
                            }
                        ]
                    }
                ],
                "max_tokens": 10,
                "temperature": 0
            }

            try:
                response = requests.post(
                    GITHUB_API_URL, headers=headers, json=payload, timeout=20
                )
                print(f"🤖 {model_name} → HTTP {response.status_code}")

                if response.status_code == 200:
                    data = response.json()
                    text = data["choices"][0]["message"]["content"].strip()
                    numbers = re.findall(r'\d+', text)
                    if numbers:
                        print(f"✅ {model_name} SOLVED: {numbers[0]}")
                        return numbers[0]
                    else:
                        print(f"⚠️ {model_name} returned no number: {text!r}")
                elif response.status_code == 401:
                    print("❌ GitHub token expired/invalid — update GITHUB_TOKEN in main.py")
                    return None
                else:
                    print(f"⚠️ {model_name} error: {response.text[:200]}")

            except Exception as e:
                print(f"⚠️ {model_name} exception: {e}")

        return None

    except Exception as e:
        print(f"AI Error: {e}")
        return None


# ============================================
# TELEGRAM CLIENT + EVENT LOOP (shared)
# ============================================
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# Shared state between Flask thread and Telegram thread
_client_loop = None          # The event loop that owns the client
_last_response = None        # Final bypassed link
_bypass_active = False       # True while a bypass attempt is in progress
_captcha_count = 0           # How many captchas solved in this session
_current_url = None          # Original URL sent — filtered out of replies


# ============================================
# TELEGRAM EVENT HANDLER
# ============================================
def _extract_bypassed_link(text, original_url):
    """
    Smart link extractor that handles the Nick Bypass Bot response format:

        Original Link : 👇
        ✅ https://original.com/...

        Bypassed Link : 👇
        ✅ https://actual-bypass.com/...

    Strategy (in order):
    1. Find the URL that appears right after "Bypassed Link" label.
    2. Among all links, skip the original URL and prefer known bypass domains.
    3. Take the last link in the message (bypassed link always comes after original).
    """
    all_links = re.findall(r'https?://[^\s\)\]>\u00ab\u00bb]+', text)
    if not all_links:
        return None

    # Strip trailing punctuation from each link
    all_links = [l.rstrip('.,;:!?') for l in all_links]

    # 1. Look for "Bypassed Link" section explicitly
    bypassed_section = re.search(
        r'(?i)bypass(?:ed)?\s*(?:link|url)\s*[:\-👇\s]+\s*(https?://[^\s\)\]>]+)',
        text
    )
    if bypassed_section:
        link = bypassed_section.group(1).rstrip('.,;:!?')
        print(f"🔍 Parsed 'Bypassed Link' section → {link}")
        return link

    # 2. Filter out the original URL, prefer known bypass indicators
    candidates = [l for l in all_links if l.rstrip('/') != (original_url or '').rstrip('/')]
    if candidates:
        for l in candidates:
            if any(k in l for k in ('generated.pages.dev', 'bypass', 'pages.dev', 'atlasclient')):
                print(f"🔍 Preferred bypass domain → {l}")
                return l
        # Take the last candidate (bypassed link comes after original in the message)
        print(f"🔍 Taking last non-original link → {candidates[-1]}")
        return candidates[-1]

    # 3. Fallback: last link in message
    print(f"🔍 Fallback: last link → {all_links[-1]}")
    return all_links[-1]


@client.on(events.NewMessage(from_users=BOT_USERNAME))
async def handler(event):
    global _last_response, _bypass_active, _captcha_count, _current_url
    msg = event.message

    print(f"📩 Msg from bot: {repr(msg.text[:80]) if msg.text else '[photo]'}")

    # ── CAPTCHA IMAGE ──────────────────────────────────────────────
    if msg.photo:
        _captcha_count += 1
        print(f"🧩 Captcha #{_captcha_count} received — solving with GitHub AI...")
        try:
            image_data = await client.download_media(msg.photo, bytes)
            if image_data:
                number = solve_captcha_github_ai(image_data)
                if number:
                    print(f"✅ Captcha #{_captcha_count} answer: {number} — sending...")
                    await client.send_message(BOT_USERNAME, number)
                    print(f"📤 Answer sent. Waiting for bot reply...")
                else:
                    print("❌ AI could not solve captcha — no answer sent")
        except Exception as e:
            print(f"❌ Captcha handler error: {e}")

    # ── TEXT RESPONSE ──────────────────────────────────────────────
    elif msg.text and _bypass_active:
        text = msg.text
        best = _extract_bypassed_link(text, _current_url)
        if best:
            _last_response = best
            print(f"🎉 BYPASSED LINK: {best}")
        else:
            print(f"💬 Bot says (no link): {text[:120]}")


# ============================================
# BYPASS FUNCTION (runs in client's loop)
# ============================================
async def _do_bypass(url, timeout=90):
    """Send url to bot, handle captchas automatically, return final link."""
    global _last_response, _bypass_active, _captcha_count, _current_url

    _last_response = None
    _bypass_active = True
    _captcha_count = 0
    _current_url = url.rstrip('/')

    print(f"📤 Sending to bot: {url}")
    await client.send_message(BOT_USERNAME, url)

    start = time.time()
    while (time.time() - start) < timeout:
        if _last_response:
            _bypass_active = False
            return _last_response
        await asyncio.sleep(1)

    _bypass_active = False
    return None


# ============================================
# FLASK ROUTES
# ============================================
@app.route('/bypass', methods=['GET', 'POST'])
def bypass():
    if request.method == 'GET':
        url = request.args.get('url')
    else:
        data = request.json or {}
        url = data.get('url')

    if not url:
        return jsonify({
            'error': '❌ URL required',
            'example': '/bypass?url=https://lksfy.com/xyz'
        }), 400

    if _client_loop is None:
        return jsonify({'error': '❌ Telegram client not ready yet'}), 503

    # ── CACHE CHECK ────────────────────────────────────────────────
    cached = cache_get(url)
    if cached:
        print(f"⚡ CACHE HIT: {url}")
        return jsonify({
            'status': '✅ SUCCESS (cached)',
            'original': url,
            'bypassed': cached,
            'cache': True,
            'timestamp': time.time()
        })

    print(f"\n{'='*50}")
    print(f"🎯 TARGET: {url}")

    future = asyncio.run_coroutine_threadsafe(_do_bypass(url), _client_loop)
    try:
        result = future.result(timeout=100)
    except Exception as e:
        print(f"❌ Bypass error: {e}")
        result = None

    print(f"{'='*50}\n")

    if result:
        cache_set(url, result)   # store in cache
        return jsonify({
            'status': '✅ SUCCESS',
            'original': url,
            'bypassed': result,
            'cache': False,
            'captchas_solved': _captcha_count,
            'solver': 'GitHub AI (GPT-4o / Phi-3.5-vision)',
            'timestamp': time.time()
        })
    else:
        return jsonify({
            'status': '❌ FAILED',
            'original': url,
            'captchas_solved': _captcha_count,
            'error': 'Timeout — captcha solve failed or GitHub token expired',
            'timestamp': time.time()
        }), 500


@app.route('/status')
def status():
    # Quick token check
    try:
        r = requests.post(
            GITHUB_API_URL,
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Content-Type": "application/json"},
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "1"}], "max_tokens": 1},
            timeout=5
        )
        token_ok = r.status_code == 200
        token_status = "✅ VALID" if token_ok else f"❌ INVALID (HTTP {r.status_code})"
    except Exception:
        token_status = "⚠️ UNREACHABLE"

    return jsonify({
        'status': '🟢 ONLINE',
        'telegram_bot': BOT_USERNAME,
        'client_ready': _client_loop is not None,
        'github_token': token_status,
        'captchas_solved_total': _captcha_count,
    })


@app.route('/cache', methods=['GET'])
def view_cache():
    """Show all cached entries."""
    with _cache_lock:
        now = time.time()
        entries = []
        for url, entry in _cache.items():
            age = int(now - entry["ts"])
            remaining = max(0, CACHE_TTL - age)
            entries.append({
                'url': url,
                'bypassed': entry['bypassed'],
                'age_seconds': age,
                'expires_in_seconds': remaining,
            })
    return jsonify({
        'cache_ttl_hours': CACHE_TTL // 3600,
        'total': len(entries),
        'entries': entries
    })


@app.route('/cache/clear', methods=['GET', 'POST'])
def clear_cache():
    """Clear all cached entries."""
    with _cache_lock:
        count = len(_cache)
        _cache.clear()
    print(f"🗑️ Cache cleared — {count} entries removed")
    return jsonify({'status': '✅ Cache cleared', 'removed': count})


@app.route('/')
def home():
    stats = cache_stats()
    return f"""
    <h1>🔥 DEMON 😈 CAPTCHA BYPASS API</h1>
    <h3>🤖 BOT: Nick_Bypass_Bot &nbsp;|&nbsp; 🧠 AI: GitHub Models</h3>
    <p>🚀 <code>/bypass?url=https://lksfy.com/xyz</code></p>
    <p>📊 <code>/status</code> &nbsp;|&nbsp; 🗃️ <code>/cache</code> &nbsp;|&nbsp; 🗑️ <code>/cache/clear</code></p>
    <hr>
    <p>Cache: <b>{stats['alive_entries']}</b> live entries (TTL {stats['ttl_hours']}h)</p>
    """


# ============================================
# START TELEGRAM CLIENT IN BACKGROUND THREAD
# ============================================
async def _run_client():
    global _client_loop
    retry_delay = 5
    while True:
        try:
            await client.connect()
            print("✅ Telegram client connected!")
            _client_loop = asyncio.get_event_loop()
            await client.run_until_disconnected()
        except Exception as e:
            err = str(e)
            print(f"⚠️  Telegram error: {err}")
            is_dup = (
                "two different IP" in err
                or "AuthKeyDuplicated" in err
                or "DuplicatedError" in err
                or type(e).__name__ == "AuthKeyDuplicatedError"
            )
            if is_dup:
                wait = min(retry_delay * 2, 120)
                print(f"🔄 Session used from another IP — waiting {wait}s before retry...")
                print("   ⚠️  Make sure the same SESSION_STRING is not active elsewhere.")
                retry_delay = wait
            else:
                print(f"❌ Unhandled error — retrying in {retry_delay}s")
            await asyncio.sleep(retry_delay)
            try:
                await client.disconnect()
            except Exception:
                pass
            continue


def start_client():
    global _client_loop
    # Small random delay in production so dev and prod don't collide on connect
    import os, random
    if os.environ.get("REPLIT_DEPLOYMENT"):
        delay = random.randint(3, 8)
        print(f"🚀 Production env — waiting {delay}s before Telegram connect...")
        time.sleep(delay)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _client_loop = loop
    loop.run_until_complete(_run_client())


if __name__ == '__main__':
    t = threading.Thread(target=start_client, daemon=True)
    t.start()
    print("🔥 DEMON 😈 CAPTCHA BYPASS API STARTED!")
    app.run(host='0.0.0.0', port=8080)
