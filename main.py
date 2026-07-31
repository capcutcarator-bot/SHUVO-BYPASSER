import os
import re
import requests
from flask import Flask, request, jsonify
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import asyncio
import base64
import time
import threading
import concurrent.futures

app = Flask(__name__)

# ============================================
# CREDENTIALS
# ============================================
API_ID   = 32128791
API_HASH = "b6274d1ac3319bffcab4f9a6015167c7"
SESSION_STRING = "1BVtsOIUBuwP25Q8uE_Jf0ky2xhV2x85yyvdAHRQw1oJ1fSqrN6Kxrlh-lIO36qbc38SKwvwwor0bvioGMSK0ihoYf0fWnTR2L4HirLMYOSn9QdPkZBQ2CyD0Hqt7vt6BMUN9NXNqAwWAHVyDe6DKJlvzXQg0kOx8zTi0XKiZGPBxPOAiZvMD4gICj8IqvWf3V_o9epagyqNsnTRTk6L4YxNwkrFA_iS0Z0m4m5qK7tlV42iVk6nbupKb2IkQliiUbNxcH7gT3DLSg4ktOc7GzHOOh7VdIM8iV_O1V7D3yk-memAPhlza7TUpt0ds3owAhnRjZ9GLgORHtSjqO1nIv3qGS-o-yzs="
BOT_USERNAME = "Nick_Bypass_Bot"

# ============================================
# OPENROUTER — FREE AI VISION
# ============================================
OPENROUTER_API_KEY = "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"
OPENROUTER_URL     = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS  = [
    "nvidia/nemotron-nano-12b-v2-vl:free",               # best free vision model
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free", # omni fallback
    "google/gemma-4-27b-it:free",                         # gemma fallback
]

# ============================================
# TELETHON CLIENT
# ============================================
# NOTE: client is now created INSIDE start_client(), after the dedicated
# event loop is created and set as current. Creating it here at import time
# binds it to the wrong (main-thread) loop and causes silent hangs / timeouts
# on /bypass once it actually runs on the background thread's loop.
client: TelegramClient = None
telegram_loop: asyncio.AbstractEventLoop = None

# Thread-pool for blocking AI calls (keeps event loop free)
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

# ============================================
# BYPASS SESSION STATE
# ============================================
class BypassSession:
    def __init__(self, url: str):
        self.url            = url
        self.result: str    = None
        self.bypass_done: asyncio.Event = None
        self.attempts       = 0
        self.max_attempts   = 3

active_session: BypassSession = None   # one bypass at a time

# ============================================
# AI CAPTCHA SOLVER
# ============================================
def solve_captcha(image_data: bytes) -> str | None:
    """
    OwnCaptcha: grid of numbered boxes, one box has a special icon inside.
    Return the number written in that box, or None on failure.
    """
    try:
        img_b64 = base64.b64encode(image_data).decode("utf-8")
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://captcha-bypass-api.replit.app",
            "X-Title": "Captcha Bypass API",
        }
        prompt = (
            "This is a captcha image showing a grid of numbered boxes. "
            "Each box has a number written in it. One box ALSO contains a special "
            "image or icon (ghost, animal, arrow, pac-man, or any object) placed "
            "inside it alongside the number. All other boxes contain ONLY their "
            "number with no image. "
            "Find the box that has BOTH a number AND a special image/icon inside it. "
            "Return ONLY that number, nothing else. No explanation, just the number."
        )
        payload = {
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }],
            "max_tokens": 10,
            "temperature": 0,
        }
        for model in OPENROUTER_MODELS:
            payload["model"] = model
            try:
                resp = requests.post(OPENROUTER_URL, headers=headers,
                                     json=payload, timeout=20)
                print(f"🤖 {model} → HTTP {resp.status_code}")
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("choices"):
                        text = data["choices"][0]["message"]["content"].strip()
                        nums = re.findall(r"\d+", text)
                        if nums:
                            print(f"✅ AI SOLVED: {nums[0]}")
                            return nums[0]
                        print(f"⚠️ Non-numeric response: {text!r}")
                else:
                    print(f"⚠️ {resp.text[:150]}")
            except Exception as e:
                print(f"⚠️ {model} error: {e}")
        return None
    except Exception as e:
        print(f"AI error: {e}")
        return None


# ============================================
# CLICK CAPTCHA BUTTON
# ============================================
async def click_captcha_button(msg, number: str) -> bool:
    """Click the inline button matching `number`. Return True if clicked."""
    try:
        if msg.buttons:
            for row in msg.buttons:
                for btn in row:
                    if re.fullmatch(r"\D*" + re.escape(number) + r"\D*", btn.text.strip()):
                        await msg.click(text=btn.text.strip())
                        print(f"🖱️ CLICKED BUTTON: '{btn.text.strip()}'")
                        return True
            # No label match — try by index (number - 1)
            flat = [b.text for row in msg.buttons for b in row]
            print(f"⚠️ No label match for '{number}', buttons: {flat}")
            try:
                await msg.click(int(number) - 1)
                print(f"🖱️ CLICKED BY INDEX: {int(number) - 1}")
                return True
            except Exception as e:
                print(f"Index click error: {e}")
    except Exception as e:
        print(f"Button click error: {e}")

    # Final fallback: send as text
    await client.send_message(BOT_USERNAME, number)
    print(f"💬 SENT TEXT FALLBACK: {number}")
    return True


# ============================================
# EXTRACT BYPASS LINK FROM BOT TEXT
# ============================================
def extract_bypass_link(text: str) -> str | None:
    """Return the bypassed URL from the bot's response, or None."""
    # Priority 1: "Bypassed Link: <url>" — emoji/symbols বা যাই থাকুক handle করবে
    m = re.search(
        r"[Bb]ypass(?:ed)?\s*[Ll]ink\s*[^\n]{0,30}?(https?://[^\s\*\)\n]+)",
        text,
    )
    if m:
        return m.group(1).rstrip("*").strip()

    # Priority 2: "Original Link" লাইন বাদ দিয়ে বাকি line থেকে URL নেবে
    for line in text.split("\n"):
        if re.search(r"[Oo]riginal\s*[Ll]ink", line):
            continue
        links = re.findall(r"https?://[^\s\*\)\n]+", line)
        for link in links:
            link = link.rstrip("*").strip()
            if link:
                return link

    # Fallback: যেকোনো URL
    links = re.findall(r"https?://[^\s\*\)\n]+", text)
    for link in links:
        link = link.rstrip("*").strip()
        if link:
            return link

    return None


# ============================================
# TELEGRAM EVENT HANDLER
# ============================================
async def handler(event):
    global active_session
    msg  = event.message
    sess = active_session   # snapshot — may be None

    preview = (msg.text or "")[:60] or "📷 Photo"
    label   = f"[{sess.attempts+1}/{sess.max_attempts}]" if sess else "[idle]"
    print(f"📩 {label} Bot: {preview!r}")

    # ── CAPTCHA IMAGE ─────────────────────────────────────────────────
    # Handle captcha ALWAYS — bot can send it at any time (random re-verification).
    if msg.photo:
        t0 = time.time()
        try:
            # Download image (fast — already async)
            image_data = await client.download_media(msg.photo, bytes)
            if not image_data:
                return

            print(f"🧠 Solving captcha… (image {len(image_data)//1024}KB)")

            # *** KEY SPEED FIX: run blocking AI call in thread executor ***
            # This keeps the Telethon event loop completely free during AI call.
            loop   = asyncio.get_event_loop()
            number = await loop.run_in_executor(_executor, solve_captcha, image_data)

            elapsed = time.time() - t0
            if number:
                print(f"⚡ Captcha solved in {elapsed:.1f}s → '{number}'")
                await click_captcha_button(msg, number)

                # Only resend URL if there is an active bypass session
                await asyncio.sleep(0.3)   # tiny pause (was 2s) — bot responds fast
                if sess and sess.attempts < sess.max_attempts:
                    sess.attempts += 1
                    print(f"🔄 Resending URL (attempt {sess.attempts}/{sess.max_attempts})…")
                    await client.send_message(BOT_USERNAME, sess.url)
                elif sess:
                    print("⚠️ Max captcha attempts reached.")
                    sess.bypass_done.set()
                # If no active session (idle re-verification), button click is enough
            else:
                print(f"❌ AI failed to solve captcha after {elapsed:.1f}s")
                if sess:
                    sess.bypass_done.set()   # unblock bypass_url with no result
        except Exception as e:
            print(f"Captcha handler error: {e}")
            if sess:
                sess.bypass_done.set()
        return

    # ── TEXT / BYPASS RESPONSE ────────────────────────────────────────
    if msg.text:
        # Skip noisy status messages to keep logs clean
        if msg.text.strip() in ("**Processing...**", "Processing..."):
            return

        print(f"📝 Bot: {msg.text!r}")

        bypass_link = extract_bypass_link(msg.text)
        if bypass_link and sess:
            sess.result = bypass_link
            print(f"✅ BYPASSED: {bypass_link}")
            sess.bypass_done.set()


# ============================================
# BYPASS COROUTINE  (runs on telegram_loop)
# ============================================
async def do_bypass(url: str) -> str | None:
    global active_session

    sess = BypassSession(url)
    sess.captcha_done = asyncio.Event()
    sess.bypass_done  = asyncio.Event()
    active_session = sess

    try:
        print(f"📤 SENDING: {url}")
        await client.send_message(BOT_USERNAME, url)

        # Wait up to 120 s for the bypass link
        try:
            await asyncio.wait_for(sess.bypass_done.wait(), timeout=120)
        except asyncio.TimeoutError:
            print("⏰ Global timeout reached")

        return sess.result
    finally:
        active_session = None


# ============================================
# FLASK ROUTES
# ============================================
@app.route("/bypass", methods=["GET", "POST"])
def bypass():
    if request.method == "GET":
        url = request.args.get("url")
    else:
        url = (request.get_json(silent=True) or {}).get("url")

    if not url:
        return jsonify({"error": "❌ URL required", "example": "/bypass?url=https://..."}), 400

    if telegram_loop is None or client is None:
        return jsonify({"error": "Telegram client not ready, retry in a moment"}), 503

    print(f"\n{'='*55}\n🎯 TARGET: {url}\n{'='*55}")

    future = asyncio.run_coroutine_threadsafe(do_bypass(url), telegram_loop)
    try:
        result = future.result(timeout=130)
    except concurrent.futures.TimeoutError:
        result = None
    except Exception as e:
        return jsonify({"status": "❌ ERROR", "error": str(e)}), 500

    if result:
        return jsonify({
            "status":    "✅ SUCCESS",
            "original":  url,
            "bypassed":  result,
            "solver":    "OpenRouter / nvidia-nemotron-vl",
            "timestamp": time.time(),
        })
    return jsonify({
        "status":    "❌ FAILED",
        "original":  url,
        "error":     "Could not bypass after 3 captcha attempts",
        "timestamp": time.time(),
    }), 500


@app.route("/status")
def status():
    return jsonify({
        "status":        "🟢 ONLINE",
        "bot":           BOT_USERNAME,
        "telegram":      "✅ CONNECTED" if telegram_loop and client else "⏳ CONNECTING",
        "ai_provider":   "OpenRouter",
        "active_bypass": active_session.url if active_session else None,
    })


@app.route("/")
def home():
    return """
    <h1>🔥 DEMON 😈 CAPTCHA BYPASS API</h1>
    <h3>🤖 Bot: Nick_Bypass_Bot</h3>
    <h3>🧠 AI: OpenRouter (NVIDIA Nemotron VL)</h3>
    <p>🚀 <b>Usage:</b> <code>/bypass?url=https://get2short.com/AyZx</code></p>
    <p>📊 <b>Status:</b> <a href="/status">/status</a></p>
    <p>✅ Auto captcha solve → auto resend URL → returns bypass link</p>
    """


# ============================================
# TELEGRAM CLIENT THREAD
# ============================================
async def _run_telegram():
    await client.connect()
    if not await client.is_user_authorized():
        print("❌ Session not authorized — generate a new session string.")
        return
    print("📡 Telegram client connected.")
    client.add_event_handler(handler, events.NewMessage(from_users=BOT_USERNAME))
    await client.run_until_disconnected()


def start_client():
    global telegram_loop, client
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    telegram_loop = loop

    # Create the client HERE, now that this thread's loop is current.
    # This is the actual fix: previously the client was created at
    # import time on the main thread's default loop, then connected on
    # a different loop here — that mismatch caused /bypass to hang/timeout.
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, loop=loop)

    loop.run_until_complete(_run_telegram())


# ============================================
# ENTRY POINT
# ============================================
t = threading.Thread(target=start_client, daemon=True)
t.start()

print("🔥 DEMON 😈 CAPTCHA BYPASS API STARTED!")
port = int(os.environ.get("PORT", 5000))
app.run(host="0.0.0.0", port=port, debug=False)
