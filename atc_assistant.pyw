"""
IVAO/VATSIM ATC Assistant
=========================
AI-powered ATC assistant for virtual pilots on IVAO and VATSIM.
pip install pyaudiowpatch faster-whisper requests pywebview psutil keyring
(keyring optional; joystick uses winmm, no pygame needed)
"""

import io, json, queue, struct, threading, time, os, ctypes, sys, requests, wave, gc, re, tempfile
import logging
from logging.handlers import RotatingFileHandler

# ── PATHS / LOGGING ─────────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(BASE_DIR, "data")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
LANG_DIR   = os.path.join(BASE_DIR, "lang")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(ASSETS_DIR, exist_ok=True)
LOG_PATH       = os.path.join(DATA_DIR, "app.log")
DISCARDED_PATH = os.path.join(DATA_DIR, "discarded_transcripts.log")
CONFIG_TXT     = os.path.join(DATA_DIR, "config.txt")
FLIGHTS_DIR    = os.path.join(DATA_DIR, "Flights")
ICON_PATH      = os.path.join(ASSETS_DIR, "Logo-ATC.ico")

# Main log: append mode + rotation (5 MB × 3 backups). Keeps history of previous
# sessions so a crash doesn't wipe out the diagnostics you need to investigate it.
_app_handler = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
_app_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(funcName)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_app_handler])
log = logging.getLogger("atc")
log.info("=== IVAO/VATSIM ATC Assistant === Python %s", sys.version)

# Separate logger for transcripts that were discarded (garbage filter or AI-decided
# "not for our callsign"). Persists across sessions so you can recover a transcript
# if the app crashes right after a Whisper run.
discarded_log = logging.getLogger("atc.discarded")
discarded_log.setLevel(logging.INFO)
discarded_log.propagate = False  # don't double-write into app.log
_disc_handler = RotatingFileHandler(DISCARDED_PATH, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
_disc_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
discarded_log.addHandler(_disc_handler)

def log_discarded(reason, text, extra=None):
    """Persist a discarded transcript with FULL text (not truncated) and the reason."""
    try:
        line = f"[{reason}] {text}"
        if extra: line += f" | {extra}"
        discarded_log.info(line)
    except Exception: pass

def atomic_write_text(path, text, encoding="utf-8"):
    """Write text to `path` atomically: write to a temp file in the SAME directory,
    fsync, then os.replace. Guarantees the destination either has the old content
    or the new — never a half-written file. Critical for surviving app crashes
    or power loss mid-save without corrupting flight JSON or config.txt."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text); f.flush()
            try: os.fsync(f.fileno())
            except Exception: pass
        os.replace(tmp, path)
    except Exception:
        try: os.remove(tmp)
        except Exception: pass
        raise

for noisy in ("webview", "pywebview", "comtypes"):
    logging.getLogger(noisy).setLevel(logging.CRITICAL)

def _excepthook(t, v, tb):
    log.critical("UNCAUGHT EXCEPTION", exc_info=(t, v, tb))
    sys.__excepthook__(t, v, tb)
sys.excepthook = _excepthook

# ── IMPORTS ─────────────────────────────────────────────────────────────────────
try:
    import pyaudiowpatch as pyaudio; log.info("pyaudiowpatch OK")
except ImportError as e:
    log.critical("MISSING DEPENDENCY pyaudiowpatch: %s — run: pip install pyaudiowpatch", e)
    raise SystemExit("pip install pyaudiowpatch")
try:
    from faster_whisper import WhisperModel; log.info("faster-whisper OK")
except ImportError as e:
    log.critical("MISSING DEPENDENCY faster-whisper: %s — run: pip install faster-whisper", e)
    raise SystemExit("pip install faster-whisper")
try:
    import webview
    try:
        from importlib.metadata import version as _pkg_version
        _wv_version = _pkg_version("pywebview")
    except Exception:
        _wv_version = getattr(webview, "__version__", "unknown")
    log.info("pywebview OK %s", _wv_version)
except ImportError as e:
    log.critical("MISSING DEPENDENCY pywebview: %s — run: pip install pywebview", e)
    raise SystemExit("pip install pywebview")
try:
    import psutil; HAS_PSUTIL = True; log.info("psutil OK")
except ImportError:
    HAS_PSUTIL = False; log.warning("psutil NOT installed")
try:
    import keyring; HAS_KEYRING = True
except ImportError:
    HAS_KEYRING = False

# ── JOYSTICK via winmm.dll (no pygame, works on Python 3.14) ─────────────────────
class _JOYCAPS(ctypes.Structure):
    _fields_ = [("wMid",ctypes.c_uint16),("wPid",ctypes.c_uint16),
                ("szPname",ctypes.c_char*32),
                ("wXmin",ctypes.c_uint32),("wXmax",ctypes.c_uint32),
                ("wYmin",ctypes.c_uint32),("wYmax",ctypes.c_uint32),
                ("wZmin",ctypes.c_uint32),("wZmax",ctypes.c_uint32),
                ("wNumButtons",ctypes.c_uint32),
                ("wPeriodMin",ctypes.c_uint32),("wPeriodMax",ctypes.c_uint32)]
class _JOYINFOEX(ctypes.Structure):
    _fields_ = [("dwSize",ctypes.c_uint32),("dwFlags",ctypes.c_uint32),
                ("dwXpos",ctypes.c_uint32),("dwYpos",ctypes.c_uint32),
                ("dwZpos",ctypes.c_uint32),("dwRpos",ctypes.c_uint32),
                ("dwUpos",ctypes.c_uint32),("dwVpos",ctypes.c_uint32),
                ("dwButtons",ctypes.c_uint32),("dwButtonNumber",ctypes.c_uint32),
                ("dwPOV",ctypes.c_uint32),("dwReserved1",ctypes.c_uint32),
                ("dwReserved2",ctypes.c_uint32)]
_JOY_RETURNBUTTONS = 0x80
_JOYERR_NOERROR    = 0
try:
    _winmm = ctypes.windll.winmm; HAS_JOYSTICK = True; log.info("winmm joystick OK")
except Exception as _je:
    _winmm = None; HAS_JOYSTICK = False; log.warning("winmm unavailable: %s", _je)

def _joy_list():
    devs = []
    if not HAS_JOYSTICK: return devs
    for i in range(16):
        caps = _JOYCAPS()
        if _winmm.joyGetDevCapsA(i, ctypes.byref(caps), ctypes.sizeof(caps)) == _JOYERR_NOERROR:
            try: name = caps.szPname.decode("cp1252", errors="replace")
            except: name = f"Joystick {i}"
            devs.append({"idx": i, "name": name, "buttons": int(caps.wNumButtons)})
    return devs

def _joy_button_pressed(joy_idx, btn_idx):
    if not HAS_JOYSTICK: return False
    info = _JOYINFOEX(); info.dwSize = ctypes.sizeof(_JOYINFOEX); info.dwFlags = _JOY_RETURNBUTTONS
    if _winmm.joyGetPosEx(joy_idx, ctypes.byref(info)) == _JOYERR_NOERROR:
        return bool(info.dwButtons & (1 << btn_idx))
    return False

def apply_dark_titlebar(hwnd):
    try:
        for attr in (20, 19):
            v = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
    except Exception as e:
        log.warning("dark titlebar: %s", e)

# ── CONSTANTS ────────────────────────────────────────────────────────────────────
APP_NAME     = "IVAOATCAssistant"
CHUNK_FRAMES = 1024
PHASES = ["Clearance","Ground","Tower","Departure","Cruise","Approach","Landing","Extra"]
REPO_URL = "https://github.com/Ricardokraus/IVAO-VATSIM-ATC-Assitant"
# Whisper medium.en fine-tuned on ATCO2 + UWB-ATCC (jack-tol). English only — WER 15% vs
# 94% on stock whisper for ATC phraseology. Loaded from HF on first use (~1.5GB download).
ATC_WHISPER_REPO = "jacktol/whisper-medium.en-fine-tuned-for-ATC-faster-whisper"

VK_MAP = {
    "space":0x20,"enter":0x0D,"tab":0x09,"backspace":0x08,"escape":0x1B,
    "ctrl":0xA2,"lctrl":0xA2,"rctrl":0xA3,"alt":0xA4,"lalt":0xA4,"ralt":0xA5,
    "shift":0xA0,"lshift":0xA0,"rshift":0xA1,"capslock":0x14,
    "f1":0x70,"f2":0x71,"f3":0x72,"f4":0x73,"f5":0x74,"f6":0x75,
    "f7":0x76,"f8":0x77,"f9":0x78,"f10":0x79,"f11":0x7A,"f12":0x7B,
    "insert":0x2D,"delete":0x2E,"home":0x24,"end":0x23,
    "pageup":0x21,"pagedown":0x22,"left":0x25,"up":0x26,"right":0x27,"down":0x28,
    "num0":0x60,"num1":0x61,"num2":0x62,"num3":0x63,"num4":0x64,"num5":0x65,
    "num6":0x66,"num7":0x67,"num8":0x68,"num9":0x69,
}
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ": VK_MAP[_c.lower()] = ord(_c)
for _d in "0123456789": VK_MAP[_d] = ord(_d)

# Map UI/voice language codes to readable names the LLM understands
LANG_NAMES = {"en":"English","es":"Spanish","fr":"French","it":"Italian","pt":"Portuguese",
              "de":"German","ru":"Russian","zh":"Chinese","ja":"Japanese","th":"Thai",
              "el":"Greek","nl":"Dutch","pl":"Polish","sv":"Swedish","tr":"Turkish","auto":"the same language the controller used"}

# ── SYSTEM PROMPT (English; output language is parameterised) ─────────────────────
def build_system_prompt(ui_lang_name, voice_lang_name):
    return f"""You are an expert ATC controller and IVAO/VATSIM flight instructor.
Interpret ONE ATC transmission and return ONLY a valid JSON object. No markdown, no ```json fences.

REQUIRED JSON (exactly 5 keys):
{{
  "assigned_phase": "<Clearance|Ground|Tower|Departure|Cruise|Approach|Landing|Extra|Ignore>",
  "atc_instruction": "<one-line summary in {ui_lang_name}>",
  "quick_list": ["SQK: 5434", "FL: 060"],
  "pilot_readback": "<readback in {voice_lang_name}, MUST end with callsign>",
  "pilot_next": ""
}}

══════════════════════════════════════════════════════════════════
NO HALLUCINATION RULES (CRITICAL — break these and the output is wrong):
══════════════════════════════════════════════════════════════════
1. quick_list contains ONLY data EXPLICITLY mentioned in THIS transmission.
   - NEVER copy data from FLIGHT CONTEXT or previous instructions into quick_list.
   - If a value wasn't pronounced this turn, OMIT it. Empty list is fine.
   - Bad example: ATC says "taxi to holding point Alpha" → quick_list MUST NOT contain "TXY: M N K"
     (those taxiways weren't named).
   - Good example: ATC says "taxi via Mike, November, Kilo 2 to runway 36R"
     → quick_list = ["TXY: M N K2", "RWY: 36R"]
   - PRESERVE numbers attached to letters: "Kilo 2" → "K2", "Alpha 1" → "A1", "Bravo 3" → "B3".

2. pilot_next is EMPTY STRING by default. Only fill it when ATC's instruction
   REQUIRES a follow-up pilot-initiated call that hasn't been requested yet.
   - FILL IT when:
     * "Push and start approved" → "Ready for taxi, <callsign>"
     * "Climb FL X, report level" → "Reaching FL X, <callsign>"
     * "Hold short of XXX, I'll call you back" → "Holding short of XXX, ready when you call, <callsign>"
     * "Cleared to land, vacate next right and contact ground" → "Vacating, contact ground, <callsign>"
   - LEAVE IT EMPTY when:
     * "Taxi to holding point Alpha" — you taxi, no callback expected
     * "Cleared to land, runway 25R" — you just land
     * "Contact tower 118.1" — you just change frequency
     * Any clearance/instruction where no clear next radio call is required from the pilot
     * When you're unsure
   - DO NOT invent generic next calls like "Ground, callsign" or "Ready to taxi when cleared, callsign"
     unless the ATC instruction explicitly leads to one.

3. assigned_phase = "Ignore" — be GENEROUS with this:
   - Single-word/short replies addressed to nobody specific: "Roger", "Wilco", "Confirm",
     "Affirm", "Negative", "Standby" → Ignore.
   - Transmissions clearly addressed to OTHER aircraft (different callsign) → Ignore.
   - Pilot-side transmissions (other pilots reading back) → Ignore.
   - Static, partial words, unintelligible noise → Ignore.
   - When Ignore: all other fields = "" or [].

══════════════════════════════════════════════════════════════════
ADDRESSING — WHO IS THIS TRANSMISSION FOR?  (decide BEFORE anything else)
══════════════════════════════════════════════════════════════════
OUR CALLSIGN and aliases are in the user message. The transmission can target us in three ways:
  A. EXPLICIT MATCH: our callsign or alias is spoken (e.g. "Iberia 1234", "Air Lince 777")
     → process normally.
  B. EXPLICIT MISMATCH: a DIFFERENT callsign is spoken (e.g. "Ryanair 4521", "Speedbird 122")
     → Ignore. Do NOT translate, do NOT readback, do NOT fill any field.
  C. NO CALLSIGN SPOKEN: use the LAST INSTRUCTIONS in FLIGHT CONTEXT to decide.
     - If the transmission logically continues OUR recent exchange (e.g. we just requested
       descent and now ATC says "Roger, descend FL 200") → process as ours.
     - If the transmission is a general broadcast (ATIS update, weather) → Extra phase.
     - If it sounds like another pilot reading back to ATC → Ignore.
     - If unclear → Ignore (be conservative; we'd rather miss one than mislabel).

IMPORTANT: numbers spoken inside instructions (FL 350, heading 180, QNH 1013, frequency 121.5)
are NOT callsigns. The callsign is always the number IMMEDIATELY after the telephony name.
Example: "Iberia 1235, climb FL 350" — callsign is 1235, NOT 350, NOT 1234.
══════════════════════════════════════════════════════════════════

══════════════════════════════════════════════════════════════════
PHASES:
- Clearance: IFR clearance, route, SID, initial squawk
- Ground: taxi, pushback, engine start, holding point
- Tower: takeoff/landing clearance, line up, wind
- Departure: initial climb, departure vectors, dep freq switch
- Cruise: en-route levels, center/area freq
- Approach: descent, vectors, ILS/RNAV approach clearance
- Landing: cleared to land, vacate runway, contact ground
- Extra: doesn't fit anything else

══════════════════════════════════════════════════════════════════
QUICK_LIST FORMAT:
- "LABEL: VALUE", English aviation abbreviations ONLY. Max 6 items. No sentences.
- Labels: SQK, FL, ALT, RWY, SID, STAR, HDG, TXY, QNH, TWR, GND, DEP, APP, CTR, SPD, FREQ, ATIS
- HDG = heading in degrees ONLY (e.g. "HDG: 220"). Taxi via taxiways = TXY (e.g. "TXY: M N K2").
- ALT (feet) and FL (flight level) are MUTUALLY EXCLUSIVE — pick whichever ATC said. Never both.
- Frequencies in TWR/GND/DEP/APP/CTR/ATIS (e.g. "TWR: 118.100").

PILOT_READBACK:
- Written in {voice_lang_name}. MUST end with the aircraft callsign.
- Example: "Cleared via GOMER1B, squawk 5434, Iberia 1234".

Use FLIGHT CONTEXT only to RESOLVE references (e.g. recognize alternative airline names from notes,
keep callsign consistent). NEVER copy context values into quick_list."""

INSTRUCTOR_PROMPT = """You are a friendly IVAO/VATSIM flight instructor. Answer briefly, clearly and didactically.
Write your answer in {ui_lang_name}. Use the flight context if helpful."""

# ── CONFIG ───────────────────────────────────────────────────────────────────────
DEFAULTS = {
    "ui_lang": "en",
    "whisper_model": "small",
    "voice_lang": "auto",           # auto | en | es | ...
    "provider": "groq",             # groq | ollama
    "api_key": "",
    "api_url": "https://api.groq.com/openai/v1/chat/completions",
    "model_name": "llama-3.1-8b-instant",
    "last_device": "",
    "last_process": "",
    "toggle_key": "f9",             # global hotkey to start/stop listening
    "mic_sensitivity": "25",        # RMS gate; lower=more sensitive. Old default was 200.
    "ai_callsign_filter": "off",    # off (regex local, saves tokens) | on (AI decides via Ignore phase)
}

def load_config():
    cfg = dict(DEFAULTS)
    try:
        if os.path.exists(CONFIG_TXT):
            with open(CONFIG_TXT, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n").rstrip("\r")
                    if not line or line.startswith("#") or "=" not in line: continue
                    k, v = line.split("=", 1); cfg[k.strip()] = v
    except Exception as e:
        log.error("load_config: %s", e, exc_info=True)
    if HAS_KEYRING:
        try:
            k = keyring.get_password(APP_NAME, "api_key")
            if k: cfg["api_key"] = k
        except Exception: pass
    return cfg

def save_config(cfg):
    try:
        text = "".join(f"{k}={cfg.get(k, DEFAULTS[k])}\n" for k in DEFAULTS)
        atomic_write_text(CONFIG_TXT, text)
    except Exception as e:
        log.error("save_config: %s", e, exc_info=True)
    if HAS_KEYRING and cfg.get("api_key"):
        try: keyring.set_password(APP_NAME, "api_key", cfg["api_key"])
        except Exception: pass

# ── i18n ───────────────────────────────────────────────────────────────────────
def list_languages():
    out = []
    try:
        for fn in sorted(os.listdir(LANG_DIR)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(LANG_DIR, fn), "r", encoding="utf-8") as f:
                        d = json.load(f)
                    meta = d.get("_meta", {})
                    out.append({"code": meta.get("code", fn[:-5]),
                                "name": meta.get("name", fn[:-5]),
                                "complete": bool(meta.get("complete", False))})
                except Exception: pass
    except Exception as e:
        log.error("list_languages: %s", e)
    return out or [{"code":"en","name":"English","complete":True}]

def load_language(code):
    p = os.path.join(LANG_DIR, f"{code}.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # fallback english
        try:
            with open(os.path.join(LANG_DIR, "en.json"), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

# ── BLACK BOX (one JSON per flight in Flights/) ──────────────────────────────────
os.makedirs(FLIGHTS_DIR, exist_ok=True)

def _safe(s):
    return "".join(c for c in str(s) if c.isalnum() or c in "-_") or "FLIGHT"

class BlackBox:
    def __init__(self):
        self.path = None; self.data = None; self._lock = threading.Lock()
    def new_flight(self, plan):
        ts = time.strftime("%Y%m%d_%H%M")
        fname = f"{_safe(plan.get('callsign','FLIGHT'))}_{ts}.json"
        self.path = os.path.join(FLIGHTS_DIR, fname)
        self.data = {"plan": plan, "created": ts, "current_phase": "Ground",
                     "global_data": {},   # {"SQK":"5434","FL":"060",...} - last value wins
                     "notes": "",          # user-only, NOT sent to AI
                     "phases": {p: {"instructions": [], "list": [], "readbacks": []} for p in PHASES},
                     "events": []}
        self._save(); log.info("New flight: %s", fname); return fname
    def list_flights(self):
        try:
            files = sorted([f for f in os.listdir(FLIGHTS_DIR) if f.endswith(".json")], reverse=True)
            out = []
            for f in files:
                try:
                    with open(os.path.join(FLIGHTS_DIR, f), "r", encoding="utf-8") as fh: d = json.load(fh)
                    pl = d.get("plan", {})
                    out.append({"file": f, "callsign": pl.get("callsign","?"),
                                "dep": pl.get("dep",""), "arr": pl.get("arr",""), "created": d.get("created","")})
                except Exception: pass
            return out
        except Exception as e:
            log.error("list_flights: %s", e); return []
    def load_flight(self, fname):
        with open(os.path.join(FLIGHTS_DIR, fname), "r", encoding="utf-8") as f:
            self.data = json.load(f)
        self.path = os.path.join(FLIGHTS_DIR, fname)
        for p in PHASES:
            self.data.setdefault("phases", {}).setdefault(p, {"instructions": [], "list": [], "readbacks": []})
        # backward-compat: migrate older key_data -> global_data, ensure notes exists
        if "global_data" not in self.data: self.data["global_data"] = self.data.pop("key_data", {})
        self.data.setdefault("notes", "")
        log.info("Flight loaded: %s", fname); return self.data
    def rename_callsign(self, new_cs):
        if not self.data: return
        with self._lock:
            self.data["plan"]["callsign"] = new_cs
            old = self.path; ts = self.data.get("created", time.strftime("%Y%m%d_%H%M"))
            new_path = os.path.join(FLIGHTS_DIR, f"{_safe(new_cs)}_{ts}.json")
            self._save()
            if old and os.path.abspath(old) != os.path.abspath(new_path):
                try: os.replace(old, new_path); self.path = new_path; log.info("Renamed to %s", os.path.basename(new_path))
                except Exception as e: log.error("rename: %s", e)
    @staticmethod
    def _parse_kv(item):
        """'SQK: 5434' -> ('SQK', '5434'). Returns (None, None) if not LABEL:VALUE."""
        s = str(item).strip()
        if ":" in s:
            k, v = s.split(":", 1)
            k = k.strip().upper(); v = v.strip()
            # strip leading "1." numbering if AI slipped it in
            if k and k[0].isdigit() and "." in k: k = k.split(".",1)[1].strip()
            if k and v and len(k) <= 8: return k, v
        return None, None
    def add_message(self, parsed):
        if not self.data: return False
        phase = parsed.get("assigned_phase", "Extra")
        if phase == "Ignore":
            log.info("Skipped (not for our callsign)")
            return False
        if phase not in PHASES: phase = "Extra"
        with self._lock:
            instr = parsed.get("atc_instruction", "")
            lst = parsed.get("quick_list", [])
            rb = parsed.get("pilot_readback", "")
            nxt = parsed.get("pilot_next", "")
            self.data["current_phase"] = phase
            ph = self.data["phases"][phase]
            if instr: ph["instructions"].append(instr)
            for it in lst:
                if it not in ph["list"]: ph["list"].append(it)
            if rb: ph["readbacks"].append(rb)
            # update global LABEL:VALUE table (last value wins) + ALT/FL exclusion
            for it in lst:
                k, v = self._parse_kv(it)
                if not k: continue
                # ALT and FL are mutually exclusive — whichever arrives last replaces the other
                if k == "ALT": self.data["global_data"].pop("FL", None)
                elif k == "FL": self.data["global_data"].pop("ALT", None)
                self.data["global_data"][k] = v
            self.data["events"].append({"t": time.strftime("%H:%M:%S"), "phase": phase,
                                        "raw": parsed.get("raw_text", instr),
                                        "instr": instr, "list": list(lst),
                                        "readback": rb, "next": nxt})
            self._save()
            return True
    def update_global(self, new_dict):
        if not self.data: return
        with self._lock:
            clean = {}
            for k, v in (new_dict or {}).items():
                k2 = str(k).strip().upper(); v2 = str(v).strip()
                if k2 and v2: clean[k2] = v2
            # if user kept both ALT and FL, drop ALT (FL wins as the conservative aviation default above transition)
            if "ALT" in clean and "FL" in clean: clean.pop("ALT")
            self.data["global_data"] = clean
            self._save()
    def update_notes(self, text):
        if not self.data: return
        with self._lock:
            self.data["notes"] = str(text or "")
            self._save()
    def context_for_ai(self):
        if not self.data: return ""
        pl = self.data.get("plan", {}); phase = self.data.get("current_phase", "Ground")
        gd = self.data.get("global_data", {})
        # When AI does callsign filtering, give it more history to detect implicit continuations
        n = 6 if _S.cfg.get("ai_callsign_filter","off") == "on" else 3
        last = self.data.get("events", [])[-n:]
        gd_str = ", ".join(f"{k}={v}" for k,v in gd.items()) if gd else "none"
        notas = (pl.get("notas") or "").strip()
        lines = [f"PLAN: {pl.get('callsign','?')} {pl.get('tipo','IFR')} {pl.get('dep','')}->{pl.get('arr','')} rwy/SID: {pl.get('pista_sid','')}",
                 f"CURRENT PHASE: {phase}",
                 f"GLOBAL DATA SO FAR: {gd_str}"]
        if notas:
            lines.append(f"PILOT NOTES / ALIASES (use to resolve unusual callsigns/airlines): {notas}")
        lines.append("LAST INSTRUCTIONS (use these to recognize implicit replies that don't repeat the callsign):")
        for e in last: lines.append(f"  [{e['t']} {e['phase']}] {e['raw'] or e['instr']}")
        return "\n".join(lines)
    def snapshot(self): return self.data
    def update_plan(self, **kw):
        if not self.data: return
        old_cs = self.data.get("plan", {}).get("callsign", "")
        new_cs = kw.get("callsign", old_cs)
        self.data["plan"].update({k:v for k,v in kw.items() if k != "callsign"})
        if new_cs and new_cs != old_cs: self.rename_callsign(new_cs)
        else: self._save()
    def _save(self):
        if not self.path or not self.data: return
        try:
            text = json.dumps(self.data, ensure_ascii=False, indent=2)
            atomic_write_text(self.path, text)
        except Exception as e:
            log.error("blackbox save: %s", e, exc_info=True)

# ── AI CLIENT (Groq / Ollama / any OpenAI-compatible) ────────────────────────────
class AIClient:
    def __init__(self, api_key, api_url, model_name, provider="groq"):
        self.api_key = api_key
        self.api_url = api_url or DEFAULTS["api_url"]
        self.model_name = model_name or DEFAULTS["model_name"]
        self.provider = provider
    def _ollama_native_url(self):
        """Normalize any Ollama URL to the native /api/chat endpoint.
        The OpenAI-compatible /v1/chat/completions endpoint IGNORES options like
        num_gpu, so we must use the native API to force CPU-only inference."""
        u = (self.api_url or "").rstrip("/")
        # Common cases: strip /v1/chat/completions or /v1 suffix; ensure /api/chat
        for suf in ("/v1/chat/completions", "/v1/completions", "/v1", "/api/chat", "/api/generate"):
            if u.endswith(suf): u = u[:-len(suf)]; break
        return u + "/api/chat"
    def generate(self, system_prompt, user_text, json_mode=True):
        headers = {"Content-Type": "application/json"}
        if self.api_key: headers["Authorization"] = f"Bearer {self.api_key}"

        if self.provider == "ollama":
            # Native Ollama API — options.num_gpu=0 ONLY works here, not on /v1/...
            url = self._ollama_native_url()
            body = {
                "model": self.model_name,
                "messages": [{"role":"system","content":system_prompt},
                             {"role":"user","content":user_text}],
                "stream": False,
                "options": {"temperature": 0.2, "num_gpu": 0},
            }
            if json_mode: body["format"] = "json"
            r = requests.post(url, headers=headers, json=body, timeout=120)
            r.raise_for_status()
            data = r.json()
            return data.get("message", {}).get("content", "") or data.get("response", "")

        # Groq / other OpenAI-compatible providers
        body = {"model": self.model_name,
                "messages": [{"role":"system","content":system_prompt},
                             {"role":"user","content":user_text}],
                "temperature": 0.2}
        if json_mode: body["response_format"] = {"type": "json_object"}
        r = requests.post(self.api_url, headers=headers, json=body, timeout=60)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

def parse_ai_json(raw):
    if not raw: raise ValueError("empty response")
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"): s = s[4:]
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1: s = s[i:j+1]
    data = json.loads(s)
    phase = data.get("assigned_phase", "Extra")
    if phase != "Ignore" and phase not in PHASES: phase = "Extra"
    return {"assigned_phase": phase,
            "atc_instruction": str(data.get("atc_instruction","")).strip(),
            "quick_list": [str(x) for x in data.get("quick_list", []) if str(x).strip()],
            "pilot_readback": str(data.get("pilot_readback","")).strip(),
            "pilot_next": str(data.get("pilot_next","")).strip()}

# ── CALLSIGN MATCHER (local, near-zero cost) ────────────────────────────────────
# Quickly decides whether an ATC transcript likely targets OUR callsign before
# we spend any AI tokens. Hybrid: digit/spoken-digit match + telephony/alias match.

_DIGIT_WORDS = {  # English + Spanish + "niner" aviation variant
    "zero":"0","cero":"0","oh":"0",
    "one":"1","uno":"1","una":"1",
    "two":"2","dos":"2",
    "three":"3","tree":"3","tres":"3",
    "four":"4","fower":"4","cuatro":"4",
    "five":"5","fife":"5","cinco":"5",
    "six":"6","seis":"6",
    "seven":"7","siete":"7",
    "eight":"8","ait":"8","ocho":"8",
    "nine":"9","niner":"9","nueve":"9",
}
# Compound English number words 10-99 that ATC may use for grouped callsign reading
# ("twelve thirty-four" = 12 34 = 1234). We expand them into digit pairs before matching.
_TEEN_TENS = {
    "ten":"10","eleven":"11","twelve":"12","thirteen":"13","fourteen":"14","fifteen":"15",
    "sixteen":"16","seventeen":"17","eighteen":"18","nineteen":"19",
    "twenty":"20","thirty":"30","forty":"40","fifty":"50","sixty":"60",
    "seventy":"70","eighty":"80","ninety":"90",
}
# Common ICAO airline telephony names (kept short — extend via plan.notas)
_TELEPHONY = {
    "IBE":"iberia","RYR":"ryanair","VLG":"vueling","AEA":"europa",
    "AFR":"airfrans","AFL":"aeroflot","BAW":"speedbird","DLH":"lufthansa",
    "AAL":"american","UAL":"united","DAL":"delta","KLM":"klm",
    "EZY":"easy","WZZ":"wizzair","TAP":"airportugal","SWR":"swiss",
    "LXR":"lynxray","CFE":"flagship",
}
def _digitize(text):
    """Convert spelled digits/teens/tens in text to digits, then combine adjacent
    single digits into one number.
       'iberia one two three four'      -> 'iberia 1234'
       'iberia twelve thirty four'      -> 'iberia 1234'  (12 + 34)
       'iberia twenty-three forty-five' -> 'iberia 2345'  (23 + 45)
    """
    # Normalize hyphens (thirty-four → thirty four)
    text = text.lower().replace("-", " ").replace("_", " ")
    raw = []
    for w in re.findall(r"[a-záéíóúñü]+|\d+|[^\w\s]", text, flags=re.UNICODE):
        if w in _DIGIT_WORDS:        raw.append(_DIGIT_WORDS[w])
        elif w in _TEEN_TENS:        raw.append(_TEEN_TENS[w])
        else:                         raw.append(w)
    # Combine "tens + unit" pairs: 30 + 4 → 34, 20 + 3 → 23, etc.
    combined = []; i = 0
    while i < len(raw):
        tok = raw[i]
        nxt = raw[i+1] if i+1 < len(raw) else None
        if (tok.isdigit() and len(tok) == 2 and tok.endswith("0") and int(tok) >= 20
                and nxt and nxt.isdigit() and len(nxt) == 1):
            combined.append(str(int(tok) + int(nxt))); i += 2; continue
        combined.append(tok); i += 1
    # Then concatenate consecutive 1-2 digit tokens into one number.
    out = []; buf = []
    for t in combined:
        if t.isdigit() and len(t) <= 2:
            buf.append(t)
        else:
            if buf: out.append("".join(buf)); buf = []
            out.append(t)
    if buf: out.append("".join(buf))
    return " ".join(out)

_TELEPH_MODIFIERS = {"heavy", "super"}

class CallsignMatcher:
    """Decides if a transcript targets our callsign. Cheap. No AI."""
    def __init__(self, callsign, notes=""):
        self.cs = (callsign or "").strip().upper()
        self.icao = self.cs[:3] if len(self.cs) >= 3 and self.cs[:3].isalpha() else ""
        self.num = "".join(c for c in self.cs if c.isdigit())
        teleph = set()
        if self.icao in _TELEPHONY: teleph.add(_TELEPHONY[self.icao])
        if self.icao: teleph.add(self.icao.lower())
        for tok in re.findall(r"[A-Za-zÁÉÍÓÚÑÜáéíóúñü]{3,}", notes or ""):
            teleph.add(tok.lower())
        self.teleph = teleph
        log.info("CallsignMatcher: cs=%s num=%s aliases=%s", self.cs, self.num, sorted(teleph))

    def _addressed_callsigns(self, tokens):
        """Find every (telephony, number) pair where the number is the FIRST
        non-modifier token after a telephony word, within 2 tokens. These are the
        actual callsigns being addressed."""
        all_teleph = set(_TELEPHONY.values()) | self.teleph
        pairs = []
        for i, tok in enumerate(tokens):
            if tok in all_teleph:
                for j in range(i+1, min(i+3, len(tokens))):
                    if tokens[j] in _TELEPH_MODIFIERS:
                        continue
                    if tokens[j].isdigit():
                        pairs.append((tok, tokens[j]))
                    break  # first non-modifier token decides
        return pairs

    def match(self, transcript):
        """'match' | 'no_match' | 'ambiguous'"""
        if not transcript or not self.cs: return "ambiguous"
        text = _digitize(transcript)
        tokens = re.findall(r"[a-z0-9]+", text)
        if not tokens: return "ambiguous"

        # 1. Are there explicit "TELEPHONY NUMBER" callsign mentions? If so, decide on them alone.
        addressed = self._addressed_callsigns(tokens)
        if addressed:
            for tele, num in addressed:
                if tele in self.teleph and num == self.num:
                    return "match"
            return "no_match"  # someone else was explicitly named, not us

        # 2. No explicit callsign mentions. Look at general signals.
        has_our_num = bool(self.num) and self.num in tokens
        has_teleph  = any(w in tokens for w in self.teleph)
        other_airline = any(n in tokens for k, n in _TELEPHONY.items()
                            if k != self.icao and n not in self.teleph)

        if other_airline and not has_teleph: return "no_match"
        if has_our_num: return "match"
        if has_teleph: return "match"  # "Iberia, say again"

        # 3. Nothing decisive — let the AI judge for short utterances, drop long ones.
        if len(tokens) < 5: return "ambiguous"
        return "no_match"

# ── LOOPBACK AUDIO CAPTURE ───────────────────────────────────────────────────────
# Windows WASAPI loopback through pyaudiowpatch. Robust against missing/changed
# devices and weird sample rates reported by drivers.
def _find_default_loopback(p):
    """Return device info dict for the system default output's loopback, or None."""
    try:
        info = p.get_default_wasapi_loopback()
        if info and info.get("isLoopbackDevice", False):
            return info
    except Exception as e:
        log.warning("get_default_wasapi_loopback failed: %s", e)
    # Manual fallback: find the default WASAPI output, then its matching loopback
    try:
        host_count = p.get_host_api_count()
        wasapi_host = None
        for h in range(host_count):
            try:
                hi = p.get_host_api_info_by_index(h)
                if "wasapi" in str(hi.get("name", "")).lower():
                    wasapi_host = hi; break
            except Exception: pass
        if wasapi_host:
            default_out_idx = wasapi_host.get("defaultOutputDevice", -1)
            if default_out_idx >= 0:
                out_info = p.get_device_info_by_index(default_out_idx)
                out_name = str(out_info.get("name", "")).lower()
                # Loopback companion shares the output device name + "[Loopback]"
                for i in range(p.get_device_count()):
                    try:
                        di = p.get_device_info_by_index(i)
                        if di.get("isLoopbackDevice", False) and out_name in str(di.get("name", "")).lower():
                            return di
                    except Exception: pass
    except Exception as e:
        log.warning("manual default-loopback lookup failed: %s", e)
    # Last resort: first loopback device that exists
    try:
        for i in range(p.get_device_count()):
            try:
                di = p.get_device_info_by_index(i)
                if di.get("isLoopbackDevice", False):
                    return di
            except Exception: pass
    except Exception: pass
    return None

def _try_open_stream(p, device_index, channels, sr):
    """Try opening a WASAPI loopback stream with the given params. Returns stream or None."""
    try:
        s = p.open(format=pyaudio.paInt16, channels=channels, rate=sr,
                   input=True, input_device_index=device_index,
                   frames_per_buffer=CHUNK_FRAMES)
        return s
    except Exception as e:
        log.warning("p.open(ch=%d, sr=%d, idx=%d) failed: %s", channels, sr, device_index, e)
        return None

def open_capture_stream(p, device_index, target_pid=None):
    """Open a loopback capture stream. Returns (stream, channels, sample_rate, used_process_bool).
    Robustly falls back to the system default loopback if the requested device is bad or
    not actually a loopback device. Tries several sample rates if the native one rejects."""
    info = None
    try:
        info = p.get_device_info_by_index(int(device_index))
    except Exception as e:
        log.warning("get_device_info_by_index(%s) failed: %s", device_index, e)

    # If invalid OR not a loopback device, find the system default loopback
    if (not info) or (not info.get("isLoopbackDevice", False)):
        log.warning("Device idx %s is not a valid loopback; switching to default loopback",
                    device_index)
        info = _find_default_loopback(p)
        if not info:
            raise RuntimeError("No WASAPI loopback device available")
        device_index = int(info["index"])
        log.info("Using default loopback: [%d] %s", device_index, info.get("name", "?"))

    native_ch = int(info.get("maxInputChannels", 2)) or 2
    native_sr = int(info.get("defaultSampleRate", 48000)) or 48000
    name = info.get("name", "?")
    log.info("Loopback target: [%d] %s | native ch=%d sr=%d",
             device_index, name, native_ch, native_sr)

    # Try native first, then common WASAPI rates, then mono fallback
    channels = min(native_ch, 2)
    rates_to_try = []
    for r in (native_sr, 48000, 44100, 32000, 22050, 16000):
        if r and r not in rates_to_try: rates_to_try.append(r)

    for ch in (channels, 1):
        for sr in rates_to_try:
            s = _try_open_stream(p, device_index, ch, sr)
            if s is not None:
                log.info("Loopback stream opened: ch=%d sr=%d device=[%d] %s",
                         ch, sr, device_index, name)
                return s, ch, sr, False

    raise RuntimeError(f"Could not open loopback stream on device [{device_index}] {name}")

# ── HOTKEY ENGINE ────────────────────────────────────────────────────────────────
# Global keyboard hotkey that fires a callback on each press (edge-triggered).
# Used to toggle the listening state without focusing the window.
class HotkeyEngine:
    def __init__(self, push_fn):
        self._push = push_fn; self._running = False
        self.vk_codes = [0x78]  # F9 default
        self._on_press = None
    def configure(self, key_name="f9"):
        parts = [p.strip().lower() for p in str(key_name).split("+") if p.strip()]
        codes = [VK_MAP.get(p, 0) for p in parts]
        codes = [c for c in codes if c]
        self.vk_codes = codes if codes else [0x78]
    def start(self, on_press):
        self._on_press = on_press
        if self._running: return
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()
    def stop(self): self._running = False
    def _is_down(self):
        try:
            gks = ctypes.windll.user32.GetAsyncKeyState
            return all(bool(gks(c) & 0x8000) for c in self.vk_codes)
        except Exception as e:
            log.warning("Hotkey poll: %s", e); return False
    def _loop(self):
        was = False
        while self._running:
            try:
                down = self._is_down()
                if down and not was:
                    was = True
                    cb = self._on_press
                    if cb:
                        try: cb()
                        except Exception as e: log.error("Hotkey callback: %s", e, exc_info=True)
                elif not down and was:
                    was = False
                time.sleep(0.04)
            except Exception as e:
                log.warning("Hotkey loop: %s", e); time.sleep(0.2)

# ── GLOBAL STATE ─────────────────────────────────────────────────────────────────
class _State: pass
_S = _State()
_S.window=None; _S.q=queue.Queue(); _S.cfg=load_config(); _S.audio_queue=queue.Queue()
_S.is_listening=False; _S.whisper_model=None; _S.whisper_size=None; _S.devices=[]
_S.bb=BlackBox(); _S.target_pid=None
_S.skipped=[]   # last N transcriptions discarded by the filter — for manual recovery
SKIPPED_MAX=30
def _push(t,d): _S.q.put({"type":t,"data":d})
_S.hotkey=HotkeyEngine(_push)
_S.hotkey.configure(_S.cfg.get("toggle_key","f9"))

# ── API ─────────────────────────────────────────────────────────────────────────
class Api:
    def _push(self,t,d): _push(t,d)
    def poll_updates(self):
        out=[]
        while not _S.q.empty():
            try: out.append(_S.q.get_nowait())
            except queue.Empty: break
        return out

    # languages
    def list_languages(self): return {"languages": list_languages()}
    def get_language(self, code):
        return {"strings": load_language(code or _S.cfg.get("ui_lang","en"))}

    # config
    def get_config(self): return dict(_S.cfg)
    def save_field(self, key, value):
        try: _S.cfg[key]=value; save_config(_S.cfg); return {"ok":True}
        except Exception as e: log.error("save_field %s: %s", key, e); return {"error":str(e)}
    def save_api_config(self, provider, api_key, api_url, model_name):
        _S.cfg["provider"]=provider
        # Local providers (Ollama, etc.) don't use an API key — clear it to avoid confusion.
        _S.cfg["api_key"]= "" if provider!="groq" else (api_key or "")
        _S.cfg["api_url"]=api_url or DEFAULTS["api_url"]; _S.cfg["model_name"]=model_name or DEFAULTS["model_name"]
        save_config(_S.cfg); return {"ok":True}
    def save_toggle_key(self, key):
        _S.cfg["toggle_key"]=key or "f9"
        save_config(_S.cfg); _S.hotkey.configure(_S.cfg["toggle_key"])
        return {"ok":True}
    def capture_hotkey(self, timeout=6.0):
        """Listens for up to 4 simultaneous keys, returns combo string like 'lctrl+lshift+d'.
        Records keys while user presses, finalizes when ALL are released or timeout.
        Skips generic modifier aliases (ctrl/shift/alt) and dedupes by VK code so that
        pressing left-ctrl doesn't register as both 'ctrl' and 'lctrl'."""
        gks = ctypes.windll.user32.GetAsyncKeyState
        # Generic aliases share VK codes with the L-variants — exclude them at capture time
        GENERIC = {"ctrl", "shift", "alt"}
        watch = [(name, vk) for name, vk in VK_MAP.items() if name not in GENERIC]
        ever_down = []          # ordered, deduped
        seen_codes = set()      # vk codes already added
        t0 = time.time()
        # Wait for first key
        while time.time() - t0 < timeout:
            for name, vk in watch:
                if gks(vk) & 0x8000 and vk not in seen_codes:
                    ever_down.append(name); seen_codes.add(vk)
                    if len(ever_down) >= 4: break
            if ever_down: break
            time.sleep(0.02)
        if not ever_down: return {"error":"timeout","combo":""}
        # Keep capturing as long as ANY key still held — accumulate more keys
        last_release = time.time()
        while time.time() - last_release < 0.4:
            any_down = False
            for name, vk in watch:
                if gks(vk) & 0x8000:
                    any_down = True
                    if vk not in seen_codes and len(ever_down) < 4:
                        ever_down.append(name); seen_codes.add(vk)
            if any_down: last_release = time.time()
            time.sleep(0.02)
        # Order modifiers first for readability
        order = {"lctrl":0,"rctrl":0,"lshift":1,"rshift":1,"lalt":2,"ralt":2}
        ever_down.sort(key=lambda k: (order.get(k, 9), k))
        combo = "+".join(ever_down)
        return {"ok":True,"combo":combo}

    # flights
    def list_flights(self): return {"flights": _S.bb.list_flights()}
    def new_flight(self, callsign, tipo, dep, arr, pista_sid, notas):
        plan={"callsign":callsign or "FLIGHT","tipo":tipo or "IFR","dep":dep,"arr":arr,"pista_sid":pista_sid,"notas":notas}
        fname=_S.bb.new_flight(plan); return {"ok":True,"file":fname,"snapshot":_S.bb.snapshot()}
    def load_flight(self, fname):
        try: return {"ok":True,"snapshot":_S.bb.load_flight(fname)}
        except Exception as e: log.error("load_flight: %s", e, exc_info=True); return {"error":str(e)}
    def get_snapshot(self): return {"snapshot":_S.bb.snapshot()}
    def update_plan(self, callsign, tipo, dep, arr, pista_sid, notas):
        if not _S.bb.data: return {"error":"no active flight"}
        _S.bb.update_plan(callsign=callsign, tipo=tipo, dep=dep, arr=arr, pista_sid=pista_sid, notas=notas)
        return {"ok":True}
    def update_global_data(self, items):
        """items: list of {label,value} or dict"""
        if not _S.bb.data: return {"error":"no active flight"}
        d = {}
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict): d[it.get("label","")] = it.get("value","")
        elif isinstance(items, dict):
            d = items
        _S.bb.update_global(d); return {"ok":True}
    def update_notes(self, text):
        if not _S.bb.data: return {"error":"no active flight"}
        _S.bb.update_notes(text); return {"ok":True}
    def get_skipped(self):
        return {"skipped": list(_S.skipped)}
    def reprocess_skipped(self, idx):
        try: idx=int(idx)
        except: return {"error":"bad idx"}
        if not (0<=idx<len(_S.skipped)): return {"error":"oob"}
        item=_S.skipped.pop(idx)
        self._push("skipped_changed",{"count":len(_S.skipped)})
        threading.Thread(target=self._call_ai, args=(item["text"],), kwargs={"force":True}, daemon=True).start()
        return {"ok":True}
    def clear_skipped(self):
        _S.skipped.clear()
        self._push("skipped_changed",{"count":0})
        return {"ok":True}

    # devices / processes / joysticks
    def get_devices(self):
        _S.devices=[]
        default_idx = -1
        try:
            p=pyaudio.PyAudio()
            # Find the system default output's loopback (the one IVAO/MSFS is probably using)
            try:
                d_info = _find_default_loopback(p)
                if d_info: default_idx = int(d_info["index"])
            except Exception as e: log.warning("default loopback lookup: %s", e)
            # List ALL loopback devices via the proper pyaudiowpatch generator when available
            seen = set()
            try:
                gen = p.get_loopback_device_info_generator()
                for info in gen:
                    idx = int(info.get("index", -1))
                    if idx < 0 or idx in seen: continue
                    seen.add(idx)
                    _S.devices.append({
                        "idx": idx,
                        "name": info["name"],
                        "is_default": (idx == default_idx),
                    })
            except Exception as e:
                log.warning("loopback generator unavailable (%s); manual scan", e)
                for i in range(p.get_device_count()):
                    try:
                        info=p.get_device_info_by_index(i)
                        if info.get("isLoopbackDevice", False) and i not in seen:
                            seen.add(i)
                            _S.devices.append({
                                "idx": i, "name": info["name"],
                                "is_default": (i == default_idx),
                            })
                    except Exception: pass
            # Reorder: default loopback first so it's the obvious pick
            _S.devices.sort(key=lambda d: (not d.get("is_default", False), d["name"]))
            p.terminate()
        except Exception as e:
            log.error("get_devices: %s", e, exc_info=True); return {"error":str(e),"devices":[]}
        # No loopback at all? Last-resort fallback to physical inputs (mic)
        if not _S.devices:
            try:
                p=pyaudio.PyAudio()
                for i in range(p.get_device_count()):
                    try:
                        info=p.get_device_info_by_index(i)
                        if info.get("maxInputChannels",0)>0:
                            _S.devices.append({"idx":i,"name":info["name"],"is_default":False})
                    except Exception: pass
                p.terminate()
            except Exception as e: log.error("devices fallback: %s", e)
        log.info("get_devices: %d loopback device(s), default idx=%d", len(_S.devices), default_idx)
        return {"devices":_S.devices,"last_device":_S.cfg.get("last_device","")}
    def get_processes(self):
        if not HAS_PSUTIL: return {"error":"psutil not installed","processes":[]}
        HINTS={"ivap","ivao","altitude","aurora","xpilot","vpilot","euroscope","swift",
               "fsx","prepar3d","p3d","xplane","x-plane","msfs","flightsimulator","fs2020","fs9"}
        procs,seen=[],set()
        try:
            for proc in psutil.process_iter(["pid","name"]):
                try:
                    pid,name=proc.info["pid"],proc.info["name"] or ""
                    if not name or pid in seen: continue
                    seen.add(pid)
                    procs.append({"pid":pid,"name":name,"highlight":any(h in name.lower() for h in HINTS)})
                except (psutil.NoSuchProcess, psutil.AccessDenied): pass
            procs.sort(key=lambda x:(0 if x["highlight"] else 1, x["name"].lower())); procs=procs[:120]
        except Exception as e:
            log.error("get_processes: %s", e); return {"error":str(e),"processes":[]}
        return {"processes":procs,"last_process":_S.cfg.get("last_process","")}
    def get_joysticks(self):
        try: return {"joysticks":_joy_list()}
        except Exception as e: log.error("get_joysticks: %s", e); return {"error":str(e),"joysticks":[]}

    def open_logs(self):
        try:
            os.startfile(DATA_DIR)  # opens the data folder where app.log lives
            return {"ok":True}
        except Exception as e:
            log.error("open_logs: %s", e); return {"error":str(e),"path":LOG_PATH}
    def get_repo_url(self): return {"url": REPO_URL}
    def open_url(self, url):
        try:
            import webbrowser; webbrowser.open(url); return {"ok":True}
        except Exception as e:
            log.error("open_url: %s", e); return {"error":str(e)}

    # whisper
    def _resolve_whisper_model_id(self, wm, voice_lang):
        """Map a whisper_model setting to the actual model id/repo to load.
        'atc-en'  -> always the ATC fine-tuned English model (HF repo, faster-whisper format)
        'atc-auto'-> ATC fine-tuned model when the configured voice language is English,
                     otherwise falls back to the multilingual base model (small)."""
        if wm == "atc-en":
            return ATC_WHISPER_REPO
        if wm == "atc-auto":
            return ATC_WHISPER_REPO if voice_lang == "en" else "small"
        return wm
    def _ensure_whisper(self):
        wm_cfg = _S.cfg.get("whisper_model","small")
        voice_lang = _S.cfg.get("voice_lang","auto")
        wm = self._resolve_whisper_model_id(wm_cfg, voice_lang)
        if _S.whisper_model is None or _S.whisper_size!=wm:
            # Release old model BEFORE loading new one — prevents the previous model from
            # lingering in RAM while the new one is being downloaded/loaded.
            if _S.whisper_model is not None:
                log.info("Swapping Whisper '%s' -> '%s'", _S.whisper_size, wm)
                _S.whisper_model=None; _S.whisper_size=None; gc.collect()
            self._push("status",{"key":"status_loading_whisper","arg":wm,"color":"amber"})
            try:
                _S.whisper_model=WhisperModel(wm, device="cpu", compute_type="int8"); _S.whisper_size=wm
                log.info("Whisper '%s' OK (setting=%s, voice_lang=%s)", wm, wm_cfg, voice_lang)
            except Exception as e:
                log.error("Whisper: %s", e, exc_info=True); return str(e)
        return None
    def _voice_lang(self):
        l=_S.cfg.get("voice_lang","auto")
        # The ATC fine-tune is an English-only (.en) model — it has no multilingual
        # detection head, so we must pass language="en" explicitly or transcription degrades.
        if _S.whisper_size==ATC_WHISPER_REPO: return "en"
        return None if l=="auto" else l
    def _resolve_pid(self):
        """Find PID by saved process NAME (so it survives restarts with new PIDs)."""
        name=_S.cfg.get("last_process","")
        if not name or not HAS_PSUTIL: return None
        try:
            for proc in psutil.process_iter(["pid","name"]):
                if (proc.info["name"] or "")==name: return proc.info["pid"]
        except Exception: pass
        return None

    # listen
    def start_listening(self, device_idx):
        if not _S.cfg.get("api_key") and _S.cfg.get("provider")=="groq":
            return {"error":"__err_no_api__"}
        try: device_idx=int(device_idx)
        except: return {"error":"__err_invalid_device__"}
        err=self._ensure_whisper()
        if err: return {"error":f"Whisper: {err}"}
        _S.target_pid=self._resolve_pid()
        _S.is_listening=True
        self._push("status",{"key":"status_listening","color":"green"})
        self._push("listening",{"active":True})
        threading.Thread(target=self._capture_loop, args=(device_idx,), daemon=True).start()
        threading.Thread(target=self._process_loop, daemon=True).start()
        return {"ok":True}
    def stop_listening(self):
        _S.is_listening=False
        self._push("status",{"key":"status_stopped","color":"dim"})
        self._push("listening",{"active":False})
        return {"ok":True}
    def toggle_listening(self, device_idx=None):
        """Start/stop based on current state. Used by the global toggle hotkey AND
        callable from the UI to share one entry point."""
        if _S.is_listening: return self.stop_listening()
        # If a device idx wasn't provided (hotkey case), reuse the last saved one
        if device_idx is None:
            device_idx = _S.cfg.get("_last_used_device_idx")
            if device_idx is None:
                # Fall back to system default loopback
                try:
                    p = pyaudio.PyAudio(); di = _find_default_loopback(p)
                    p.terminate()
                    if di: device_idx = int(di["index"])
                except Exception as e: log.warning("toggle default device: %s", e)
            if device_idx is None: return {"error":"__err_no_device__"}
        # Remember last device used so hotkey works after a manual start
        _S.cfg["_last_used_device_idx"] = int(device_idx)
        return self.start_listening(device_idx)

    # manual
    def send_message(self, text, is_question):
        text=(text or "").strip()
        if not text: return {"error":"__err_empty__"}
        if not _S.cfg.get("api_key") and _S.cfg.get("provider")=="groq": return {"error":"__err_no_api__"}
        if is_question: threading.Thread(target=self._ask_instructor, args=(text,), daemon=True).start()
        else:
            self._push("transcript",{"text":text})
            threading.Thread(target=self._call_ai, args=(text,), daemon=True).start()
        return {"ok":True}

    # AI
    def _sys_prompt(self):
        ui=LANG_NAMES.get(_S.cfg.get("ui_lang","en"),"English")
        voice=LANG_NAMES.get(_S.cfg.get("voice_lang","auto"),"the same language the controller used")
        return build_system_prompt(ui, voice)
    def _call_ai(self, text, force=False):
        """If force=True, bypass local filter AND will never be added to skipped buffer."""
        pl = (_S.bb.data or {}).get("plan", {})
        ai_filter = _S.cfg.get("ai_callsign_filter", "off") == "on"
        if not force and not ai_filter:
            try:
                cs = pl.get("callsign", ""); notes = pl.get("notas", "")
                if cs:
                    m = CallsignMatcher(cs, notes).match(text)
                    if m == "no_match":
                        log.info("Local filter skipped (not for %s): %s", cs, text[:80])
                        log_discarded("local_filter_no_callsign", text, f"our_callsign={cs}")
                        self._add_skipped(text, "local filter (no callsign match)")
                        self._push("transcript_skip", {"text": text})
                        self._status_idle(); return
                    log.info("Local filter: %s for transcript: %s", m, text[:60])
            except Exception as e:
                log.warning("local filter error (failing open): %s", e)
        elif ai_filter and not force:
            log.info("AI-based callsign filter active — local regex bypassed")
        self._push("status",{"key":"status_consulting_ai","color":"amber"})
        try:
            ctx=_S.bb.context_for_ai()
            user=f"OUR CALLSIGN: {pl.get('callsign','?')}\n\nFLIGHT CONTEXT:\n{ctx}\n\nTOWER TRANSMISSION:\n{text}"
            client=AIClient(_S.cfg.get("api_key"),_S.cfg.get("api_url"),_S.cfg.get("model_name"),_S.cfg.get("provider","groq"))
            raw=client.generate(self._sys_prompt(), user, json_mode=True)
            parsed=parse_ai_json(raw)
            parsed["raw_text"] = text
            stored = _S.bb.add_message(parsed)
            if stored:
                self._push("atc_message", parsed)
            else:
                if not force:
                    self._add_skipped(text, "AI marked as Ignore")
                log_discarded("ai_ignore", text, f"our_callsign={pl.get('callsign','?')}")
                self._push("transcript_skip", {"text": text})
        except requests.exceptions.ConnectionError as e:
            log.error("_call_ai: connection error: %s", e, exc_info=True)
            prov = _S.cfg.get("provider", "groq")
            self._push("toast", {"key": "err_ollama_down" if prov == "ollama" else "err_ai_connection",
                                  "kind": "error"})
        except requests.exceptions.Timeout as e:
            log.error("_call_ai: timeout: %s", e, exc_info=True)
            self._push("toast", {"key": "err_ai_timeout", "kind": "error"})
        except Exception as e:
            log.error("_call_ai: %s", e, exc_info=True)
            self._push("toast",{"raw":f"AI error: {e}","kind":"error"})
        self._status_idle()
    def _add_skipped(self, text, reason):
        ts = time.strftime("%H:%M:%S")
        _S.skipped.insert(0, {"t": ts, "text": text, "reason": reason})
        del _S.skipped[SKIPPED_MAX:]
        self._push("skipped_changed", {"count": len(_S.skipped)})
    def _ask_instructor(self, text):
        self._push("status",{"key":"status_consulting_instructor","color":"amber"})
        try:
            ui=LANG_NAMES.get(_S.cfg.get("ui_lang","en"),"English")
            ctx=_S.bb.context_for_ai()
            client=AIClient(_S.cfg.get("api_key"),_S.cfg.get("api_url"),_S.cfg.get("model_name"),_S.cfg.get("provider","groq"))
            reply=client.generate(INSTRUCTOR_PROMPT.format(ui_lang_name=ui),
                                   f"CONTEXT:\n{ctx}\n\nPILOT QUESTION:\n{text}", json_mode=False)
            self._push("instructor",{"question":text,"answer":reply.strip()})
        except requests.exceptions.ConnectionError as e:
            log.error("_ask_instructor: connection error: %s", e, exc_info=True)
            prov = _S.cfg.get("provider", "groq")
            self._push("toast", {"key": "err_ollama_down" if prov == "ollama" else "err_ai_connection",
                                  "kind": "error"})
        except requests.exceptions.Timeout as e:
            log.error("_ask_instructor: timeout: %s", e, exc_info=True)
            self._push("toast", {"key": "err_ai_timeout", "kind": "error"})
        except Exception as e:
            log.error("_ask_instructor: %s", e, exc_info=True)
            self._push("toast",{"raw":f"Error: {e}","kind":"error"})
        self._status_idle()
    def _status_idle(self):
        active=_S.is_listening
        self._push("status",{"key":"status_listening" if active else "status_stopped","color":"green" if active else "dim"})

    # audio
    # Phrases Whisper commonly hallucinates on silence/non-speech audio. Never send to AI.
    _WHISPER_HALLUCINATIONS = (
        "subtitles by", "subtitulos por", "subtítulos por", "subtitle", "subs by",
        "thanks for watching", "thank you for watching", "gracias por ver",
        "amara.org", "transcript", "transcrip", ". . .", "...",
    )
    def _is_garbage_transcript(self, text):
        """Reject empty/too-short/hallucinated transcripts BEFORE involving the LLM.
        Saves tokens, removes a class of phantom instructions, and avoids the case
        where the AI invents data from nothing."""
        t = (text or "").strip()
        if len(t) < 3: return True
        # Strip punctuation/dots to see if anything real is there
        alnum = "".join(ch for ch in t if ch.isalnum())
        if len(alnum) < 3: return True
        low = t.lower()
        for h in self._WHISPER_HALLUCINATIONS:
            if h in low: return True
        return False
    def _transcribe_async(self, raw, channels, sr):
        threading.Thread(target=self._transcribe, args=(raw,channels,sr), daemon=True).start()
    def _transcribe(self, raw, channels, sr):
        self._push("status",{"key":"status_transcribing","color":"amber"})
        wav=io.BytesIO()
        with wave.open(wav,"wb") as wf:
            wf.setnchannels(channels); wf.setsampwidth(2); wf.setframerate(sr); wf.writeframes(raw)
        wav.seek(0)
        try:
            segs,_i=_S.whisper_model.transcribe(wav, language=self._voice_lang(), beam_size=5)
            text=" ".join(s.text for s in segs).strip()
        except Exception as e:
            log.error("transcribe: %s", e, exc_info=True)
            self._push("toast",{"raw":f"Whisper: {e}","kind":"error"}); return
        if self._is_garbage_transcript(text):
            log.info("Discarding empty/hallucinated transcript: %r", text[:80])
            log_discarded("garbage", text)
            self._status_idle(); return
        self._push("transcript",{"text":text}); self._call_ai(text)
    def _capture_loop(self, device_index):
        p=pyaudio.PyAudio()
        try:
            stream, channels, sr, used = open_capture_stream(p, device_index, _S.target_pid)
            fpc=int(sr*4); buf,collected=[],0; got_audio=False; checks=0
            sens = float(_S.cfg.get("mic_sensitivity", 25))  # RMS gate; lower = more sensitive
            last_vu_push = 0
            # Diagnostic: track peak RMS over a 10s window and log it so silence is visible
            diag_peak = 0.0; diag_pushed = 0; diag_t0 = time.time()
            log.info("Capture loop started: device=%d ch=%d sr=%d gate=%.1f", device_index, channels, sr, sens)
            while _S.is_listening:
                try: data=stream.read(CHUNK_FRAMES, exception_on_overflow=False)
                except Exception as e:
                    log.warning("stream.read error: %s", e); time.sleep(0.05); continue

                # Real-time VU: compute RMS of THIS small chunk and push frequently
                chunk_samples = struct.unpack(f"{len(data)//2}h", data) if data else ()
                chunk_rms = (sum(s*s for s in chunk_samples)/len(chunk_samples))**0.5 if chunk_samples else 0
                if chunk_rms > diag_peak: diag_peak = chunk_rms
                now = time.time()
                if now - last_vu_push > 0.08:  # ~12 Hz
                    self._push("vu", {"rms": round(chunk_rms, 1), "gate": sens})
                    last_vu_push = now

                # Accumulate 4s window for transcription gating
                buf.append(data); collected+=CHUNK_FRAMES
                if collected>=fpc:
                    raw=b"".join(buf)
                    samples=struct.unpack(f"{len(raw)//2}h", raw)
                    rms=(sum(s*s for s in samples)/len(samples))**0.5 if samples else 0
                    if rms>sens*0.6: got_audio=True
                    if rms>sens: _S.audio_queue.put((raw,channels,sr)); diag_pushed+=1
                    buf,collected=[],0; checks+=1
                    # Every 10s, log the peak so silent-loopback is obvious in app.log
                    if now-diag_t0>10:
                        log.info("Capture diag (10s): peak_rms=%.1f gate=%.1f chunks_pushed=%d",
                                 diag_peak, sens, diag_pushed)
                        if diag_peak < 1.0:
                            log.warning("LOOPBACK IS SILENT — verify Windows is playing audio "
                                        "through device [%d]; check default output device", device_index)
                        diag_peak=0.0; diag_pushed=0; diag_t0=now
                    # if using process loopback and no audio after ~20s, fall back to system
                    if used and checks==5 and not got_audio:
                        log.warning("Process loopback silent; falling back to system audio")
                        try: stream.stop_stream(); stream.close()
                        except Exception: pass
                        stream, channels, sr, used = open_capture_stream(p, device_index, None)
                        self._push("toast",{"raw":"Process audio silent, using system audio","kind":"error"})
            stream.stop_stream(); stream.close()
        except Exception as e:
            log.error("_capture_loop: %s", e, exc_info=True)
            self._push("toast",{"raw":f"Audio: {e}","kind":"error"})
        finally:
            p.terminate()
    def _process_loop(self):
        while _S.is_listening:
            try: raw,channels,sr=_S.audio_queue.get(timeout=1)
            except queue.Empty: continue
            self._transcribe(raw,channels,sr)
    def close_app(self):
        _S.is_listening=False
        try: _S.hotkey.stop()
        except Exception: pass
        if _S.window:
            try: _S.window.destroy()
            except Exception: pass
    def restart_app(self):
        """Relaunch the process, then tear down this one. Used after a UI-language
        change since some elements only re-render correctly on a fresh load.
        Returns {"restarted": True} if a new process was spawned, or
        {"restarted": False} if relaunch wasn't possible (caller should tell the
        user to reopen manually before close_app is invoked)."""
        restarted = False
        try:
            import subprocess
            if getattr(sys, "frozen", False):
                cmd = [sys.executable] + [a for a in sys.argv[1:] if a != "--restart"] + ["--restart"]
            else:
                cmd = [sys.executable, os.path.abspath(__file__)] + \
                      [a for a in sys.argv[1:] if a != "--restart"] + ["--restart"]
            subprocess.Popen(cmd, cwd=BASE_DIR,
                              creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            restarted = True
            log.info("Relaunched: %s", cmd)
        except Exception as e:
            log.error("restart_app: relaunch failed, will close only: %s", e, exc_info=True)
        # Tear down this instance either way (new process is already starting up)
        threading.Timer(0.4, self.close_app).start()
        return {"restarted": restarted}

HTML = r"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>ATC Assistant</title>
<style>
:root{--bg:#101113;--bg2:#181a1d;--bg3:#212429;--bg4:#2a2e34;--border:#2e3237;--border2:#3a3f45;
--fg:#eceef0;--fg2:#969ca3;--fg3:#5c6168;--accent:#4a9eff;--accent2:#3a8aee;--accentdim:#1c3454;
--green:#3dba6e;--amber:#f0a500;--red:#f25555;--purple:#b78aff;--radius:9px;
--font:"Segoe UI",system-ui,sans-serif;--mono:"Cascadia Code","Consolas",monospace;}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--fg);font-family:var(--font);font-size:14px;-webkit-font-smoothing:antialiased}
::-webkit-scrollbar{width:6px;height:6px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
/* Custom range slider: single hover target (the thumb), track fills edge-to-edge,
   thicker for easier grabbing. Browser defaults give the track and thumb separate
   hover/focus rings and inset the thumb from the ends — both fixed here.
   ".mf input[type=range]" (higher specificity than ".mf input") neutralizes the
   generic field padding/border/background so the slider isn't boxed in. */
input[type="range"],.mf input[type="range"]{-webkit-appearance:none;appearance:none;
  width:100%;height:10px;background:var(--bg4);border:none;border-radius:6px;
  outline:none;cursor:pointer;margin:0;padding:0}
input[type="range"]::-webkit-slider-runnable-track{height:10px;border-radius:6px;background:var(--bg4)}
input[type="range"]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;
  width:22px;height:22px;border-radius:50%;background:var(--accent);border:2px solid var(--bg);
  margin-top:-6px;cursor:pointer;transition:transform .08s ease,background .08s ease;
  box-shadow:0 1px 3px rgba(0,0,0,.4)}
input[type="range"]:hover::-webkit-slider-thumb{background:var(--accent2);transform:scale(1.12)}
input[type="range"]:active::-webkit-slider-thumb{transform:scale(1.0)}
input[type="range"]:focus-visible::-webkit-slider-thumb{box-shadow:0 0 0 3px var(--accentdim)}
.mf input[type="range"]:focus,.mf input[type="range"]:focus-visible{box-shadow:none;border-color:transparent}
input[type="range"]::-moz-range-track{height:10px;border-radius:6px;background:var(--bg4);border:none}
input[type="range"]::-moz-range-thumb{width:22px;height:22px;border-radius:50%;
  background:var(--accent);border:2px solid var(--bg);cursor:pointer;
  transition:transform .08s ease,background .08s ease;box-shadow:0 1px 3px rgba(0,0,0,.4)}
input[type="range"]:hover::-moz-range-thumb{background:var(--accent2);transform:scale(1.12)}
input[type="range"]:active::-moz-range-thumb{transform:scale(1.0)}
#app{display:flex;flex-direction:column;height:100%;user-select:none}
/* menubar */
#menubar{display:flex;background:var(--bg2);border-bottom:1px solid var(--border);flex-shrink:0;height:32px;position:relative;z-index:600;padding:0 4px}
.mi{position:relative;display:flex;align-items:center;padding:0 14px;cursor:pointer;font-size:12px;color:var(--fg2);border-radius:5px;margin:3px 1px;transition:background .12s,color .12s}
.mi:hover,.mi.open{background:var(--bg4);color:var(--fg)}
.dd{display:none;position:absolute;top:34px;background:var(--bg3);border:1px solid var(--border2);border-radius:var(--radius);min-width:210px;padding:5px;z-index:9999;box-shadow:0 14px 40px rgba(0,0,0,.65)}
.dd.show{display:block}
.di{padding:8px 14px;border-radius:6px;cursor:pointer;font-size:12px;color:var(--fg);white-space:nowrap;transition:background .1s}
.di:hover{background:var(--accent);color:#fff}.dsep{height:1px;background:var(--border);margin:4px 10px}
#main{flex:1;overflow:hidden;padding:11px 13px;display:flex;flex-direction:column;gap:9px}
/* control bar */
#ctrlbar{display:flex;align-items:center;gap:11px;flex-shrink:0}
.bigbtn{display:inline-flex;align-items:center;justify-content:center;gap:9px;padding:13px 30px;border-radius:11px;border:none;font-size:15px;font-weight:700;cursor:pointer;transition:background .15s;font-family:var(--font);min-width:160px}
.bigbtn-go{background:var(--accent);color:#fff}.bigbtn-go:hover{background:var(--accent2)}.bigbtn-go:disabled{background:var(--accentdim);color:#4a6385;cursor:default}
.bigbtn-stop{background:#4a1a1a;color:#ffabab;border:1px solid #7a2424}.bigbtn-stop:hover{background:#5e2222}.bigbtn-stop:disabled{background:#241616;color:#5a3a3a;border-color:#321c1c;cursor:default}
.gear{width:46px;height:46px;border-radius:11px;border:1px solid var(--border2);background:var(--bg3);color:var(--fg2);font-size:19px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;transition:background .15s,color .15s;flex-shrink:0}
.gear:hover{background:var(--bg4);color:var(--fg)}
.ctrlinfo{display:flex;flex-direction:column;gap:2px;min-width:0}
#statusLine{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--fg2);font-weight:600}
.dot{width:9px;height:9px;border-radius:50%;background:var(--fg3);flex-shrink:0}
#statusLine.green{color:var(--green)}#statusLine.green .dot{background:var(--green)}
#statusLine.amber{color:var(--amber)}#statusLine.amber .dot{background:var(--amber)}
#statusLine.red{color:var(--red)}#statusLine.red .dot{background:var(--red)}
#deviceInfo{font-size:12px;color:var(--fg3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:360px}
#vuBar{position:relative;width:160px;height:8px;background:var(--bg2);border:1px solid var(--border);border-radius:4px;overflow:hidden}
#vuFill{position:absolute;top:0;left:0;height:100%;width:0%;background:linear-gradient(to right,var(--green),var(--amber) 70%,var(--red));transition:width .05s linear}
#vuGate{position:absolute;top:-2px;width:2px;height:12px;background:var(--accent);transition:left .3s}
#skipBadge{position:absolute;top:2px;right:2px;min-width:18px;height:18px;border-radius:9px;background:var(--amber);color:#000;font-size:10.5px;font-weight:700;display:flex;align-items:center;justify-content:center;padding:0 5px}
#flightTag{margin-left:auto;font-size:12.5px;color:var(--fg3);text-align:right;line-height:1.4}#flightTag b{color:var(--accent)}
/* global panel */
#globalPanel{flex-shrink:0;display:grid;grid-template-columns:1fr 1fr;gap:9px}
.gpcol{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:9px 12px;min-height:74px}
.gptitle{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--fg3);margin-bottom:6px}
.gpitem{font-size:12px;line-height:1.45;padding:2px 0;border-bottom:1px solid var(--border);word-break:break-word}.gpitem:last-child{border-bottom:none}
.gpcol.atc .gpitem{color:var(--accent)}.gpcol.rb .gpitem{color:var(--green)}
.gpempty{font-size:12.5px;color:var(--fg3);font-style:italic}
/* tabs */
#tabsRow{flex-shrink:0;display:flex;gap:4px;flex-wrap:wrap}
.tab{padding:7px 14px;border-radius:7px 7px 0 0;background:var(--bg2);border:1px solid var(--border);border-bottom:none;color:var(--fg3);font-size:12px;font-weight:600;cursor:pointer;position:relative;transition:color .12s,background .12s}
.tab:hover{color:var(--fg2);background:var(--bg3)}.tab.active{background:var(--bg3);color:var(--accent);border-color:var(--border2)}.tab.has-data{color:var(--fg)}
.tab .ndot{position:absolute;top:4px;right:5px;width:7px;height:7px;border-radius:50%;background:var(--accent);display:none}.tab.flash .ndot{display:block}
#tabContent{flex:1;min-height:0;background:var(--bg3);border:1px solid var(--border2);border-radius:0 9px 9px 9px;padding:0;overflow:hidden;display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:0}
#tabLeft{overflow-y:auto;padding:13px;border-right:1px solid var(--border)}
#tabRight{overflow-y:auto;padding:13px;display:flex;flex-direction:column;gap:13px;background:var(--bg2)}
.iblock{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:10px}
.iblock-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;border-bottom:1px solid var(--border);padding-bottom:6px}
.iblock-n{font-weight:700;color:var(--accent);font-size:12px;letter-spacing:.03em}
.iblock-t{font-size:12.5px;color:var(--fg3);font-family:var(--mono)}
.iblock-sec{margin-top:7px}
.iblock-sec:first-child{margin-top:0}
.iblock-lbl{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--fg3);margin-bottom:4px;display:flex;align-items:center;gap:6px}
.iblock-lbl .ic{width:10px;height:10px;border-radius:2px}
.iblock-txt{font-size:12.5px;line-height:1.5;color:var(--fg);padding:3px 0;word-break:break-word}
.iblock-txt.atc{color:var(--fg)}
.iblock-txt.rb{color:var(--green)}
.iblock-li{font-size:12px;line-height:1.5;color:var(--fg);padding:2px 0;display:inline-block;background:var(--bg3);border-radius:4px;padding:2px 8px;margin:2px 4px 2px 0;font-family:var(--mono);font-size:12.5px}
.rcol{background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:11px 13px}
.rcol h3{font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--fg3);margin-bottom:9px;display:flex;align-items:center;gap:7px}
.rcol .hint{font-size:12px;color:var(--fg3);margin-top:6px}
.gd-row{display:grid;grid-template-columns:60px minmax(0,1fr) 22px;gap:6px;align-items:start;margin-bottom:5px}
.gd-row input.gd-k,.gd-row textarea{background:var(--bg2);color:var(--fg);border:1px solid var(--border);border-radius:5px;padding:5px 8px;font-size:12px;font-family:var(--mono);outline:none;transition:border-color .12s;width:100%;box-sizing:border-box}
.gd-row textarea{resize:none;min-height:28px;line-height:1.35;overflow:hidden;word-break:break-word;white-space:pre-wrap;font-size:12px}
.gd-row input.gd-k{text-align:center}
.gd-row input:focus,.gd-row textarea:focus{border-color:var(--accent)}
.gd-row .gd-k{color:var(--accent);font-weight:700;text-transform:uppercase}
.gd-row .gd-del{width:22px;height:28px;border:none;background:transparent;color:var(--fg3);cursor:pointer;border-radius:4px;font-size:13px;transition:background .12s,color .12s;margin-top:0}
.gd-row .gd-del:hover{background:var(--bg4);color:var(--red)}
#gdAdd{margin-top:7px;width:100%;background:var(--bg2);color:var(--fg2);border:1px dashed var(--border2);border-radius:6px;padding:6px;font-size:12.5px;cursor:pointer;font-family:var(--font);transition:border-color .12s,color .12s}
#gdAdd:hover{border-color:var(--accent);color:var(--accent)}
#notesArea{width:100%;min-height:140px;background:var(--bg2);color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:9px 11px;font-size:12px;font-family:var(--font);line-height:1.5;outline:none;resize:vertical;transition:border-color .12s}
#notesArea:focus{border-color:var(--accent)}
.sectitle{font-size:12px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--fg3);margin-bottom:7px;display:flex;align-items:center;gap:7px}
.ic{width:14px;height:14px;border-radius:3px;display:inline-block}.ic-atc{background:var(--accent)}.ic-list{background:var(--amber)}.ic-rb{background:var(--green)}
.itm{font-size:13px;line-height:1.55;padding:6px 10px;border-radius:6px;background:var(--bg2);margin-bottom:5px;word-break:break-word}
.itm.atc{border-left:3px solid var(--accent)}.itm.rb{border-left:3px solid var(--green)}.itm.li{border-left:3px solid var(--amber)}
.empty{font-size:12px;color:var(--fg3);font-style:italic;padding:4px 0}
/* manual */
#manualBar{flex-shrink:0;display:flex;gap:9px;align-items:center;background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);padding:8px 11px}
#manualBar input{flex:1;background:var(--bg3);color:var(--fg);border:1px solid var(--border2);border-radius:6px;padding:7px 11px;font-size:12px;outline:none;transition:border-color .15s}
#manualBar input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(74,158,255,.15)}
.toggle{display:inline-flex;align-items:center;gap:7px;cursor:pointer;color:var(--fg2);font-size:12.5px;font-weight:600;white-space:nowrap}
.toggle input{display:none}.toggle .track{width:34px;height:18px;background:var(--bg4);border-radius:10px;position:relative;transition:background .2s;border:1px solid var(--border2)}
.toggle .thumb{position:absolute;top:1px;left:1px;width:14px;height:14px;border-radius:50%;background:var(--fg2);transition:transform .2s,background .2s}
.toggle input:checked + .track{background:var(--accent);border-color:var(--accent)}.toggle input:checked + .track .thumb{transform:translateX(16px);background:#fff}
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 17px;border-radius:7px;border:none;font-size:12px;font-weight:600;cursor:pointer;transition:background .15s;font-family:var(--font)}
.btn-p{background:var(--accent);color:#fff}.btn-p:hover{background:var(--accent2)}
.btn-s{background:var(--bg3);color:var(--fg);border:1px solid var(--border2)}.btn-s:hover{background:var(--bg4)}
.btn-sm{padding:5px 11px;font-size:12.5px}
/* modals */
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.8);align-items:center;justify-content:center;z-index:3000}
.overlay.show{display:flex}
.modal{background:var(--bg2);border:1px solid var(--border2);border-radius:13px;padding:26px;width:480px;max-width:92vw;box-shadow:0 28px 70px rgba(0,0,0,.75);max-height:90vh;overflow-y:auto}
.modal h2{font-size:15px;font-weight:700;margin-bottom:18px}
.mf{margin-bottom:13px}.mf label{display:block;margin-bottom:5px;color:var(--fg2);font-size:12.5px;font-weight:600}
.mf input,.mf select{width:100%;background:var(--bg3);color:var(--fg);border:1px solid var(--border2);border-radius:6px;padding:7px 10px;font-size:12px;outline:none;font-family:var(--font)}
.mf input:focus,.mf select:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(74,158,255,.15)}
.mf select{cursor:pointer;appearance:none;padding-right:26px;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%237a8088'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 9px center}
.mrow2{display:grid;grid-template-columns:1fr 1fr;gap:11px}
.ma{display:flex;gap:9px;justify-content:flex-end;margin-top:20px}
.hint{font-size:12px;color:var(--fg3);margin-top:4px;line-height:1.45}
.hint-ok{font-size:12px;color:var(--green);margin-top:4px}
.seg{display:inline-flex;background:var(--bg3);border:1px solid var(--border2);border-radius:7px;padding:2px;gap:2px;width:100%}
.seg button{flex:1;background:transparent;border:none;color:var(--fg2);padding:6px;border-radius:5px;font-size:12.5px;font-weight:600;cursor:pointer;font-family:var(--font);transition:background .12s,color .12s}
.seg button.active{background:var(--accent);color:#fff}
input[readonly]{cursor:default;color:var(--accent);font-family:var(--mono);text-align:center}
.key{background:var(--bg3);border:1px solid var(--border2);border-radius:4px;padding:1px 7px;font-family:var(--mono);font-size:12.5px;color:var(--accent)}
/* help tabs */
.htabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:14px}
.htab{padding:6px 12px;border-radius:6px;background:var(--bg3);border:1px solid var(--border2);color:var(--fg2);font-size:12.5px;font-weight:600;cursor:pointer;transition:background .12s,color .12s}
.htab:hover{background:var(--bg4);color:var(--fg)}.htab.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.hcontent{color:var(--fg2);line-height:1.8;font-size:12px;max-height:48vh;overflow-y:auto}
.hcontent p{margin-bottom:7px}.hcontent b{color:var(--fg)}
.repolink{display:inline-block;margin-top:10px;color:var(--accent);cursor:pointer;font-weight:600}.repolink:hover{text-decoration:underline}
/* startup */
#startup{position:fixed;inset:0;background:var(--bg);z-index:2000;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:22px;padding:30px}
#startup h1{font-size:22px;font-weight:800;letter-spacing:-.5px;text-align:center}
#startup .sub{font-size:13px;color:var(--fg2);text-align:center;margin-top:4px}
#startLangGear{position:fixed;top:16px;right:16px;width:40px;height:40px;border-radius:10px;border:1px solid var(--border2);background:var(--bg2);color:var(--fg2);font-size:17px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .15s,color .15s;z-index:2100}
#startLangGear:hover{background:var(--bg3);color:var(--fg)}

.startcards{display:flex;gap:16px;flex-wrap:wrap;justify-content:center}
.startcard{width:230px;background:var(--bg2);border:1px solid var(--border2);border-radius:14px;padding:24px;cursor:pointer;transition:border-color .15s,background .15s;text-align:center}
.startcard:hover{border-color:var(--accent);background:var(--bg3)}
.startcard .icn{font-size:32px;margin-bottom:10px}.startcard h3{font-size:15px;font-weight:700;margin-bottom:6px}.startcard p{font-size:12.5px;color:var(--fg2);line-height:1.5}
.backbtn{font-size:12px;color:var(--fg2);cursor:pointer;background:none;border:none;font-family:var(--font)}.backbtn:hover{color:var(--fg)}
#flightList{width:100%;max-width:520px;max-height:300px;overflow-y:auto;display:flex;flex-direction:column;gap:7px}
.flightItem{background:var(--bg2);border:1px solid var(--border2);border-radius:9px;padding:12px 15px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;transition:border-color .12s,background .12s}
.flightItem:hover{border-color:var(--accent);background:var(--bg3)}
.flightItem .cs{font-weight:700;color:var(--accent);font-size:14px}.flightItem .rt{font-size:12.5px;color:var(--fg2)}.flightItem .dt{font-size:12px;color:var(--fg3)}
.startForm{width:100%;max-width:460px;background:var(--bg2);border:1px solid var(--border2);border-radius:14px;padding:24px}
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--bg4);color:var(--fg);padding:10px 18px;border-radius:8px;font-size:12px;z-index:3000;box-shadow:0 8px 24px rgba(0,0,0,.6);display:none;border:1px solid var(--border2)}
#toast.error{border-color:var(--red);color:#ffb0b0}#toast.show{display:block}
</style></head>
<body>


<button id="startLangGear" title="Language / Idioma">&#9881;</button>

<!-- STARTUP -->
<div id="startup">
  <div><h1>IVAO/VATSIM ATC Assistant</h1><div class="sub" data-t="app_subtitle"></div></div>
  <div id="startChoice" class="startcards">
    <div class="startcard" id="cardNew"><div class="icn">&#9992;</div><h3 data-t="startup_new"></h3><p data-t="startup_new_desc"></p></div>
    <div class="startcard" id="cardLoad"><div class="icn">&#128190;</div><h3 data-t="startup_load"></h3><p data-t="startup_load_desc"></p></div>
  </div>
  <div id="startNew" class="startForm" style="display:none">
    <div class="mrow2">
      <div class="mf"><label data-t="nf_callsign"></label><input type="text" id="nf_callsign" placeholder="IBE1234"></div>
      <div class="mf"><label data-t="nf_type"></label><select id="nf_tipo"><option>IFR</option><option>VFR</option></select></div>
    </div>
    <div class="mrow2">
      <div class="mf"><label data-t="nf_dep"></label><input type="text" id="nf_dep" placeholder="GCTS"></div>
      <div class="mf"><label data-t="nf_arr"></label><input type="text" id="nf_arr" placeholder="LEMD"></div>
    </div>
    <div class="mf"><label data-t="nf_runway"></label><input type="text" id="nf_pista" placeholder="07R / GOMER 1B"></div>
    <div class="mf"><label data-t="nf_notes"></label><input type="text" id="nf_notas"><div class="hint" data-t="plan_notas_hint"></div></div>
    <div class="ma"><button class="backbtn" id="newBack" data-t="back"></button><button class="btn btn-p" id="newCreate" data-t="nf_create"></button></div>
  </div>
  <div id="startLoad" style="display:none;flex-direction:column;align-items:center;gap:14px;width:100%">
    <div id="flightList"></div><button class="backbtn" id="loadBack" data-t="back"></button>
  </div>
</div>

<!-- APP -->
<div id="app" style="display:none">
<div id="menubar">
  <div class="mi" data-menu="0" data-t="menu_flight"></div>
  <div class="mi" data-menu="1" data-t="menu_config"></div>
  <div class="mi" data-menu="2" data-t="menu_help"></div>
  <div class="dd" data-dd="0" style="left:5px">
    <div class="di" data-act="newflight" data-t="menu_new_flight"></div>
    <div class="di" data-act="loadflight" data-t="menu_load_flight"></div>
    <div class="dsep"></div>
    <div class="di" data-act="editplan" data-t="menu_edit_plan"></div>
    <div class="dsep"></div>
    <div class="di" data-act="exit" data-t="menu_exit"></div>
  </div>
  <div class="dd" data-dd="1" style="left:55px">
    <div class="di" data-act="api" data-t="menu_api"></div>
    <div class="di" data-act="audio" data-t="menu_audio"></div>
    <div class="di" data-act="lang" data-t="menu_language"></div>
    <div class="dsep"></div>
    <div class="di" data-act="log" data-t="menu_open_logs"></div>
  </div>
  <div class="dd" data-dd="2" style="left:167px"><div class="di" data-act="help" data-t="menu_how_to"></div></div>
</div>

<div id="main">
  <div id="ctrlbar">
    <button class="bigbtn bigbtn-go" id="btnStart"><span>&#9654;</span><span data-t="btn_start"></span></button>
    <button class="bigbtn bigbtn-stop" id="btnStop" disabled><span>&#9632;</span><span data-t="btn_stop"></span></button>
    <button class="gear" id="btnGear" title="Audio">&#9881;</button>
    <div class="ctrlinfo">
      <div id="statusLine"><span class="dot"></span><span data-t="status_stopped"></span></div>
      <div id="deviceInfo"></div>
      <div id="vuRow" style="display:none;align-items:center;gap:8px;margin-top:3px">
        <div id="vuBar" title="Mic level"><div id="vuFill"></div><div id="vuGate"></div></div>
      </div>
    </div>
    <button class="gear" id="btnSkipped" style="position:relative" title="Discarded transmissions">&#128172;<span id="skipBadge" style="display:none"></span></button>
    <div id="flightTag"></div>
  </div>
  <div id="globalPanel">
    <div class="gpcol atc"><div class="gptitle" data-t="global_atc"></div><div id="gpAtc"></div></div>
    <div class="gpcol rb"><div class="gptitle" data-t="global_rb"></div><div id="gpRb"></div></div>
  </div>
  <div id="tabsRow"></div>
  <div id="tabContent"></div>
  <div id="manualBar">
    <input type="text" id="manualIn">
    <label class="toggle"><input type="checkbox" id="isQ"><span class="track"><span class="thumb"></span></span><span data-t="ask_doubt"></span></label>
    <button class="btn btn-p" id="btnSend" data-t="btn_send"></button>
  </div>
</div>
</div>

<!-- MODALS -->
<div class="overlay" data-overlay="apiModal"><div class="modal">
  <h2 data-t="api_title"></h2>
  <div class="mf"><label data-t="api_provider"></label>
    <div class="seg" id="provSeg"><button data-prov="groq" class="active" data-t="api_provider_cloud"></button><button data-prov="ollama" data-t="api_provider_local"></button></div></div>
  <div class="mf" id="apiKeyRow"><label data-t="api_key"></label><input type="password" id="apiKey"></div>
  <div class="mf"><label data-t="api_url"></label><input type="text" id="apiUrl"></div>
  <div class="mf"><label data-t="api_model"></label><input type="text" id="modelName"></div>
  <div class="mf" style="border-top:1px solid var(--border);padding-top:11px;margin-top:11px">
    <label style="display:flex;align-items:center;gap:9px;cursor:pointer">
      <input type="checkbox" id="aiCallsignFilter" style="width:16px;height:16px;cursor:pointer">
      <span data-t="ai_filter_label"></span>
    </label>
    <div class="hint" data-t="ai_filter_hint"></div>
  </div>
  <div class="hint" id="apiHint"></div>
  <div class="ma"><button class="btn btn-s" data-close="apiModal" data-t="cancel"></button><button class="btn btn-p" id="btnApiSave" data-t="save"></button></div>
</div></div>

<div class="overlay" data-overlay="audioModal"><div class="modal" style="width:560px">
  <h2 data-t="audio_title"></h2>
  <div class="mf"><label data-t="audio_device"></label><select id="deviceSel"></select></div>
  <div class="mf"><label data-t="audio_process"></label><select id="processSel"></select><div class="hint" data-t="audio_process_hint"></div><div class="hint" id="procCount"></div><div class="hint" id="audioSrcBanner" style="margin-top:6px"></div></div>
  <div class="mf"><label data-t="toggle_key"></label>
    <div style="display:flex;gap:9px;align-items:center"><input type="text" id="toggleKey" readonly style="min-width:120px;width:auto;field-sizing:content;padding-right:14px;padding-left:14px;font-family:var(--mono)" value="f9">
      <button class="btn btn-s btn-sm" id="btnCapKey" data-t="toggle_key_capture"></button><span class="hint" style="margin:0" data-t="toggle_key_capture_hint"></span></div>
    <div class="hint" data-t="toggle_key_hint"></div></div>
  <div class="mf" style="margin-top:13px"><label data-t="mic_sensitivity"></label>
    <div style="display:flex;align-items:center;gap:11px">
      <input type="range" id="micSensitivity" min="0" max="200" step="1" value="25" style="flex:1">
      <span id="micSensVal" style="font-family:var(--mono);font-size:12px;min-width:40px;text-align:right">25</span>
    </div>
    <div class="hint" data-t="mic_sensitivity_hint"></div>
  </div>
  <div class="mf" style="margin-top:13px"><label data-t="lang_whisper"></label><select id="whisperModel"><option>tiny</option><option>base</option><option>small</option><option>medium</option><option value="atc-en" data-t="lang_whisper_atc_en"></option><option value="atc-auto" data-t="lang_whisper_atc_auto"></option></select><div class="hint" data-t="lang_whisper_hint"></div></div>
  <div class="ma"><button class="btn btn-s" data-close="audioModal" data-t="close"></button><button class="btn btn-p" id="btnAudioSave" data-t="save"></button></div>
</div></div>

<div class="overlay" data-overlay="langModal"><div class="modal" style="width:440px">
  <h2 data-t="lang_title"></h2>
  <div class="mf"><label data-t="lang_ui"></label><select id="uiLangSel"></select></div>
  <div class="mf"><label data-t="lang_voice"></label><select id="voiceLangSel"></select><div class="hint" data-t="lang_voice_hint"></div></div>
  <div class="ma"><button class="btn btn-s" data-close="langModal" data-t="close"></button><button class="btn btn-p" id="btnLangSave" data-t="save"></button></div>
</div></div>

<div class="overlay" data-overlay="planModal"><div class="modal">
  <h2 data-t="plan_title"></h2>
  <div class="mrow2"><div class="mf"><label data-t="nf_callsign"></label><input type="text" id="ep_callsign"></div><div class="mf"><label data-t="nf_type"></label><select id="ep_tipo"><option>IFR</option><option>VFR</option></select></div></div>
  <div class="mrow2"><div class="mf"><label data-t="nf_dep"></label><input type="text" id="ep_dep"></div><div class="mf"><label data-t="nf_arr"></label><input type="text" id="ep_arr"></div></div>
  <div class="mf"><label data-t="nf_runway"></label><input type="text" id="ep_pista"></div>
  <div class="mf"><label data-t="nf_notes"></label><input type="text" id="ep_notas"><div class="hint" data-t="plan_notas_hint"></div></div>
  <div class="hint" data-t="plan_hint"></div>
  <div class="ma"><button class="btn btn-s" data-close="planModal" data-t="cancel"></button><button class="btn btn-p" id="btnPlanSave" data-t="save"></button></div>
</div></div>

<div class="overlay" data-overlay="helpModal"><div class="modal" style="width:600px">
  <h2 data-t="help_title"></h2>
  <div class="htabs" id="helpTabs"></div>
  <div class="hcontent" id="helpContent"></div>
  <div class="ma"><button class="btn btn-p" data-close="helpModal" data-t="ok"></button></div>
</div></div>

<!-- generic confirm -->
<div class="overlay" data-overlay="skippedModal"><div class="modal" style="width:680px;max-height:78vh">
  <h2 data-t="skipped_title"></h2>
  <div class="hint" data-t="skipped_hint" style="margin-bottom:11px"></div>
  <div id="skippedList" style="overflow-y:auto;max-height:50vh;border:1px solid var(--border);border-radius:7px;padding:6px;background:var(--bg2)"></div>
  <div class="ma">
    <button class="btn btn-s" id="btnClearSkipped" data-t="skipped_clear"></button>
    <button class="btn btn-s" data-close="skippedModal" data-t="close"></button>
  </div>
</div></div>

<div class="overlay" data-overlay="confirmModal"><div class="modal" style="width:400px">
  <div id="confirmText" style="font-size:13px;line-height:1.6;margin-bottom:6px"></div>
  <div class="ma"><button class="btn btn-s" id="confirmCancel" data-t="cancel"></button><button class="btn btn-p" id="confirmOk" data-t="ok"></button></div>
</div></div>

<div id="toast"></div>

<script>
const PHASES=["Clearance","Ground","Tower","Departure","Cruise","Approach","Landing","Extra"];
let CFG={},SNAP=null,activeTab=null,STR={},gAtc=[],gRb=[],REPO="";
async function api(m,...a){try{return await window.pywebview.api[m](...a);}catch(e){console.error(m,e);return null;}}
function waitReady(cb,n=0){if(window.pywebview&&window.pywebview.api)cb();else if(n<60)setTimeout(()=>waitReady(cb,n+1),120);}
function t(k,arg){let s=STR[k]||k;if(arg!==undefined)s=s.replace("{0}",arg);return s;}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function toast(msg,kind){const el=document.getElementById('toast');el.textContent=msg;el.className=(kind==='error'?'error ':'')+'show';setTimeout(()=>el.className='',3200);}

/* i18n apply */
function applyStrings(){
  document.querySelectorAll('[data-t]').forEach(e=>{const k=e.dataset.t;e.textContent=(STR[k]!==undefined)?STR[k]:'';});
  document.getElementById('manualIn').placeholder=t('manual_placeholder');
  renderGlobal();if(activeTab)renderTab(activeTab);refreshDeviceInfo();
}
async function setUiLang(code){
  const r=await api('get_language',code);if(r&&r.strings)STR=r.strings;
  await api('save_field','ui_lang',code);CFG.ui_lang=code;applyStrings();
}

/* startup */
document.getElementById('cardNew').onclick=()=>{document.getElementById('startChoice').style.display='none';document.getElementById('startNew').style.display='block';};
document.getElementById('cardLoad').onclick=showLoadList;
async function showLoadList(){
  document.getElementById('startChoice').style.display='none';
  document.getElementById('startNew').style.display='none';
  const sl=document.getElementById('startLoad');sl.style.display='flex';
  const r=await api('list_flights');const list=document.getElementById('flightList');const fl=(r&&r.flights)||[];
  list.innerHTML=fl.length?fl.map(f=>`<div class="flightItem" data-file="${f.file}"><div><div class="cs">${esc(f.callsign)}</div><div class="rt">${esc(f.dep||'?')} &rarr; ${esc(f.arr||'?')}</div></div><div class="dt">${esc(f.created)}</div></div>`).join(''):`<div class="gpempty" style="text-align:center">${t('startup_no_flights')}</div>`;
  list.querySelectorAll('.flightItem').forEach(it=>it.onclick=()=>loadFlight(it.dataset.file));
}
document.getElementById('newBack').onclick=()=>{document.getElementById('startNew').style.display='none';document.getElementById('startChoice').style.display='flex';};
document.getElementById('loadBack').onclick=()=>{document.getElementById('startLoad').style.display='none';document.getElementById('startChoice').style.display='flex';};
document.getElementById('newCreate').onclick=async()=>{
  const cs=document.getElementById('nf_callsign').value.trim();if(!cs){toast(t('callsign_required'),'error');return;}
  const r=await api('new_flight',cs,document.getElementById('nf_tipo').value,document.getElementById('nf_dep').value.trim().toUpperCase(),document.getElementById('nf_arr').value.trim().toUpperCase(),document.getElementById('nf_pista').value.trim(),document.getElementById('nf_notas').value.trim());
  if(r&&r.ok){SNAP=r.snapshot;enterApp();}else toast('Error','error');
};
async function loadFlight(file){const r=await api('load_flight',file);if(r&&r.ok){SNAP=r.snapshot;enterApp();}else toast('Error','error');}
function backToStartup(){
  // clean return to startup without reload (keeps memory low, no blank screen)
  document.getElementById('app').style.display='none';
  document.getElementById('startup').style.display='flex';
  document.getElementById('startLoad').style.display='none';
  document.getElementById('startNew').style.display='none';
  document.getElementById('startChoice').style.display='flex';
}
function enterApp(){
  document.getElementById('startup').style.display='none';
  document.getElementById('startLangGear').style.display='none';
  document.getElementById('app').style.display='flex';
  rebuildFromSnapshot();refreshDeviceInfo();
}

/* rebuild */
function rebuildFromSnapshot(){
  buildTabs();gAtc=[];gRb=[];
  if(SNAP){const pl=SNAP.plan||{};
    document.getElementById('flightTag').innerHTML=`<b>${esc(pl.callsign||'?')}</b> ${esc(pl.tipo||'')}<br>${esc(pl.dep||'?')} &rarr; ${esc(pl.arr||'?')}`;
    (SNAP.events||[]).forEach(e=>{if(e.raw||e.instr)pushG(gAtc,e.raw||e.instr);if(e.readback)pushG(gRb,e.readback);});
    renderGlobal();
    PHASES.forEach(f=>{const ph=(SNAP.phases||{})[f]||{};markTabData(f,!!((ph.instructions||[]).length||(ph.readbacks||[]).length||(ph.list||[]).length));});
    selectTab(SNAP.current_phase||'Ground');
  }
}
function buildTabs(){const r=document.getElementById('tabsRow');r.innerHTML=PHASES.map(f=>`<div class="tab" data-tab="${f}">${f}<span class="ndot"></span></div>`).join('');r.querySelectorAll('.tab').forEach(t2=>t2.onclick=()=>selectTab(t2.dataset.tab));}
function markTabData(f,has){const t2=document.querySelector(`.tab[data-tab="${f}"]`);if(t2)t2.classList.toggle('has-data',has);}
function flashTab(f){const t2=document.querySelector(`.tab[data-tab="${f}"]`);if(t2)t2.classList.add('flash');}
function selectTab(f){activeTab=f;document.querySelectorAll('.tab').forEach(t2=>{t2.classList.toggle('active',t2.dataset.tab===f);if(t2.dataset.tab===f)t2.classList.remove('flash');});renderTab(f);}
function renderTab(f){
  // Filter events by phase, preserving order
  const events=(SNAP&&SNAP.events||[]).filter(e=>e.phase===f);
  const c=document.getElementById('tabContent');
  // Build left column: numbered instruction blocks
  let leftHtml='<div id="tabLeft">';
  if(!events.length){
    leftHtml+=`<div class="empty">${t('empty_nothing')}</div>`;
  }else{
    events.forEach((e,i)=>{
      const n=i+1;
      const liItems=(e.list||[]).map(x=>`<span class="iblock-li">${esc(x)}</span>`).join('');
      leftHtml+=`<div class="iblock"><div class="iblock-hdr"><span class="iblock-n">${t('instruction_n')} ${n}</span><span class="iblock-t">${esc(e.t||'')}</span></div>`;
      if(e.raw||e.instr)leftHtml+=`<div class="iblock-sec"><div class="iblock-lbl"><span class="ic ic-atc"></span>${t('sec_atc')}</div><div class="iblock-txt atc">${esc(e.raw||e.instr)}</div></div>`;
      if(liItems)leftHtml+=`<div class="iblock-sec"><div class="iblock-lbl"><span class="ic ic-list"></span>${t('sec_list')}</div><div>${liItems}</div></div>`;
      if(e.readback)leftHtml+=`<div class="iblock-sec"><div class="iblock-lbl"><span class="ic ic-rb"></span>${t('sec_rb')}</div><div class="iblock-txt rb">${esc(e.readback)}</div></div>`;
      if(e.next)leftHtml+=`<div class="iblock-sec"><div class="iblock-lbl"><span class="ic" style="background:var(--purple)"></span>${t('sec_next')}</div><div class="iblock-txt" style="color:var(--purple)">${esc(e.next)}</div></div>`;
      leftHtml+=`</div>`;
    });
  }
  leftHtml+='</div>';
  // Right column: global quick list (editable) + notes
  let rightHtml=`<div id="tabRight">
    <div class="rcol"><h3><span class="ic ic-list"></span>${t('right_col_global')}</h3>
      <div id="gdList"></div>
      <button id="gdAdd">+ ${t('add')}</button>
      <div class="hint">${t('global_hint')}</div>
    </div>
    <div class="rcol"><h3><span class="ic" style="background:var(--purple)"></span>${t('right_col_notes')}</h3>
      <textarea id="notesArea" placeholder="${esc(t('notes_placeholder'))}"></textarea>
    </div>
  </div>`;
  c.innerHTML=leftHtml+rightHtml;
  // Auto-scroll to bottom (latest instruction visible)
  const tl=document.getElementById('tabLeft');if(tl)tl.scrollTop=tl.scrollHeight;
  renderGlobalData();
  const ta=document.getElementById('notesArea');
  if(ta){ta.value=(SNAP&&SNAP.notes)||'';
    ta.addEventListener('input',debouncedSaveNotes);}
}
function autoGrowTA(el){el.style.height='auto';el.style.height=(el.scrollHeight)+'px';}
function renderGlobalData(){
  const list=document.getElementById('gdList');if(!list)return;
  const gd=(SNAP&&SNAP.global_data)||{};
  const keys=Object.keys(gd);
  if(!keys.length){list.innerHTML=`<div class="empty" style="font-size:12.5px">${t('global_empty')}</div>`;}
  else{
    list.innerHTML=keys.map(k=>`<div class="gd-row" data-k="${esc(k)}">
      <input type="text" class="gd-k" value="${esc(k)}" data-field="label" maxlength="8">
      <textarea rows="1" data-field="value">${esc(gd[k])}</textarea>
      <button class="gd-del" title="${t('del')}">&#10005;</button></div>`).join('');
    list.querySelectorAll('.gd-row').forEach(r=>{
      const ta=r.querySelector('textarea');if(ta){autoGrowTA(ta);ta.addEventListener('input',()=>autoGrowTA(ta));ta.addEventListener('change',saveGlobalData);}
      const ki=r.querySelector('input.gd-k');if(ki)ki.addEventListener('change',saveGlobalData);
      r.querySelector('.gd-del').onclick=()=>{r.remove();saveGlobalData();};
    });
  }
  const add=document.getElementById('gdAdd');if(add)add.onclick=()=>{
    const row=document.createElement('div');row.className='gd-row';
    row.innerHTML=`<input type="text" class="gd-k" data-field="label" maxlength="8" placeholder="${t('label')}"><textarea rows="1" data-field="value" placeholder="${t('value')}"></textarea><button class="gd-del" title="${t('del')}">&#10005;</button>`;
    list.appendChild(row);
    const ta=row.querySelector('textarea');autoGrowTA(ta);ta.addEventListener('input',()=>autoGrowTA(ta));ta.addEventListener('change',saveGlobalData);
    row.querySelector('input.gd-k').addEventListener('change',saveGlobalData);
    row.querySelector('.gd-del').onclick=()=>{row.remove();saveGlobalData();};
    row.querySelector('input').focus();
  };
}
async function saveGlobalData(){
  const rows=document.querySelectorAll('#gdList .gd-row');const items=[];
  rows.forEach(r=>{const k=r.querySelector('[data-field="label"]').value.trim().toUpperCase();const v=r.querySelector('[data-field="value"]').value.trim();if(k&&v)items.push({label:k,value:v});});
  if(SNAP){const nd={};items.forEach(it=>nd[it.label]=it.value);SNAP.global_data=nd;}
  await api('update_global_data',items);
}
let _notesTimer=null;
function debouncedSaveNotes(){clearTimeout(_notesTimer);_notesTimer=setTimeout(async()=>{const v=document.getElementById('notesArea').value;if(SNAP)SNAP.notes=v;await api('update_notes',v);},600);}
function pushG(a,v){a.push(v);while(a.length>3)a.shift();}
function renderGlobal(){
  const a=document.getElementById('gpAtc'),r=document.getElementById('gpRb');
  a.innerHTML=gAtc.length?gAtc.slice().reverse().map(x=>`<div class="gpitem">${esc(x)}</div>`).join(''):`<div class="gpempty">${t('no_instructions')}</div>`;
  r.innerHTML=gRb.length?gRb.slice().reverse().map(x=>`<div class="gpitem">${esc(x)}</div>`).join(''):`<div class="gpempty">${t('no_readbacks')}</div>`;
}
function onAtcMessage(p){
  if(SNAP){
    const ph=SNAP.phases[p.assigned_phase]||(SNAP.phases[p.assigned_phase]={instructions:[],list:[],readbacks:[]});
    if(p.atc_instruction)ph.instructions.push(p.atc_instruction);
    (p.quick_list||[]).forEach(i=>{if(!ph.list.includes(i))ph.list.push(i);});
    if(p.pilot_readback)ph.readbacks.push(p.pilot_readback);
    SNAP.current_phase=p.assigned_phase;
    // also push event (matches backend) so the new render shows it immediately
    SNAP.events=SNAP.events||[];
    SNAP.events.push({t:new Date().toLocaleTimeString('en-GB'),phase:p.assigned_phase,
      raw:p.raw_text||p.atc_instruction||'',instr:p.atc_instruction||'',list:(p.quick_list||[]).slice(),readback:p.pilot_readback||'',next:p.pilot_next||''});
    // update global_data with parsed LABEL:VALUE items (last wins, ALT/FL mutually exclusive)
    SNAP.global_data=SNAP.global_data||{};
    (p.quick_list||[]).forEach(it=>{const s=String(it);const i=s.indexOf(':');if(i>0){const k=s.slice(0,i).trim().toUpperCase();const v=s.slice(i+1).trim();if(k&&v&&k.length<=8){if(k==='ALT')delete SNAP.global_data['FL'];if(k==='FL')delete SNAP.global_data['ALT'];SNAP.global_data[k]=v;}}});
  }
  if(p.raw_text)pushG(gAtc,p.raw_text);if(p.pilot_readback)pushG(gRb,p.pilot_readback);
  renderGlobal();markTabData(p.assigned_phase,true);flashTab(p.assigned_phase);selectTab(p.assigned_phase);
}

/* menus */
const menus=[...document.querySelectorAll('.mi')],dds=[...document.querySelectorAll('.dd')];
function closeMenus(){menus.forEach(m=>m.classList.remove('open'));dds.forEach(d=>d.classList.remove('show'));}
menus.forEach(mi=>{const dd=document.querySelector(`.dd[data-dd="${mi.dataset.menu}"]`);
  mi.addEventListener('mouseenter',()=>{if(dds.some(d=>d.classList.contains('show'))){closeMenus();mi.classList.add('open');dd.classList.add('show');}});
  mi.addEventListener('mousedown',e=>{e.stopPropagation();const o=dd.classList.contains('show');closeMenus();if(!o){mi.classList.add('open');dd.classList.add('show');}});});
document.getElementById('menubar').addEventListener('mouseleave',()=>setTimeout(()=>{if(!document.querySelector('#menubar:hover')&&!document.querySelector('.dd:hover'))closeMenus();},80));
dds.forEach(dd=>dd.addEventListener('mouseleave',()=>setTimeout(()=>{if(!document.querySelector('#menubar:hover')&&!document.querySelector('.dd:hover'))closeMenus();},80)));
document.addEventListener('mousedown',e=>{if(!e.target.closest('#menubar'))closeMenus();});
document.querySelectorAll('.di').forEach(di=>di.addEventListener('mousedown',e=>{
  e.stopPropagation();const act=di.dataset.act;closeMenus();
  if(act==='exit')api('close_app');
  else if(act==='api'){openModal('apiModal');loadApiModal();}
  else if(act==='audio'){openModal('audioModal');refreshDevices();loadProcesses();}
  else if(act==='lang'){openModal('langModal');loadLangModal();}
  else if(act==='editplan')openPlanModal();
  else if(act==='newflight')confirmDo(t('confirm_new'),()=>{stopAll();backToStartup();});
  else if(act==='loadflight')confirmDo(t('confirm_load'),()=>{stopAll();backToStartup();showLoadList();});
  else if(act==='log')api('open_logs');
  else if(act==='help'){openModal('helpModal');initHelp();}
}));
function stopAll(){api('stop_listening');document.getElementById('btnStart').disabled=false;document.getElementById('btnStop').disabled=true;}

/* modals */
function openModal(id){document.querySelector(`.overlay[data-overlay="${id}"]`).classList.add('show');}
function closeModal(id){document.querySelector(`.overlay[data-overlay="${id}"]`).classList.remove('show');}
document.querySelectorAll('.overlay').forEach(ov=>ov.addEventListener('mousedown',e=>{if(e.target===ov)ov.classList.remove('show');}));
document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>closeModal(b.dataset.close)));
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeMenus();document.querySelectorAll('.overlay.show').forEach(o=>o.classList.remove('show'));}});
document.getElementById('btnGear').onclick=()=>{openModal('audioModal');refreshDevices();loadProcesses();};
document.getElementById('btnSkipped').onclick=async()=>{openModal('skippedModal');await renderSkipped();};
document.getElementById('btnClearSkipped').onclick=async()=>{await api('clear_skipped');await renderSkipped();};
async function renderSkipped(){
  const r=await api('get_skipped');const items=(r&&r.skipped)||[];
  const c=document.getElementById('skippedList');
  if(!items.length){c.innerHTML='<div class="empty" style="padding:18px;text-align:center">'+t('skipped_empty')+'</div>';return;}
  c.innerHTML=items.map((it,i)=>`<div class="skipRow" data-i="${i}" style="padding:8px 10px;border-bottom:1px solid var(--border);display:flex;gap:9px;align-items:flex-start">
    <div style="flex:1;min-width:0">
      <div style="font-family:var(--mono);font-size:11px;color:var(--fg3);margin-bottom:3px">${esc(it.t)} — ${esc(it.reason)}</div>
      <div style="font-size:13px;color:var(--fg);word-break:break-word">${esc(it.text)}</div>
    </div>
    <button class="btn btn-s btn-sm" data-act="reprocess">${t('skipped_reprocess')}</button>
  </div>`).join('');
  c.querySelectorAll('.skipRow').forEach(r=>{
    r.querySelector('[data-act=reprocess]').onclick=async()=>{
      const i=parseInt(r.dataset.i);await api('reprocess_skipped',i);await renderSkipped();
      toast(t('skipped_reprocessing'));
    };
  });
}

/* confirm modal */
let _confirmCb=null;
function confirmDo(msg,cb){document.getElementById('confirmText').textContent=msg;_confirmCb=cb;openModal('confirmModal');}
document.getElementById('confirmOk').onclick=()=>{closeModal('confirmModal');if(_confirmCb)_confirmCb();};
document.getElementById('confirmCancel').onclick=()=>closeModal('confirmModal');

/* config */
async function loadCfg(){const c=await api('get_config');if(!c)return;CFG=c;
  document.getElementById('whisperModel').value=c.whisper_model||'small';
  document.getElementById('toggleKey').value=c.toggle_key||'f9';
  const ms=document.getElementById('micSensitivity');if(ms){ms.value=c.mic_sensitivity||'25';document.getElementById('micSensVal').textContent=ms.value;ms.oninput=()=>document.getElementById('micSensVal').textContent=ms.value;}
}
function refreshDeviceInfo(){
  const proc=CFG.last_process||'';
  const dev=CFG.last_device||t('device_none');
  const el=document.getElementById('deviceInfo');
  if(proc){el.textContent=t('process_label')+': '+proc;el.title=t('device_label')+': '+dev;}
  else{el.textContent=t('device_label')+': '+dev;el.title='';}
}

/* API modal */
let curProv='groq';
function applyProvVisibility(){
  // Local providers (Ollama, etc.) need no API key. Hide the field to remove ambiguity
  // about phantom saved credentials.
  const isLocal=(curProv!=='groq');
  const row=document.getElementById('apiKeyRow');
  if(row)row.style.display=isLocal?'none':'';
  document.getElementById('apiHint').textContent=t(isLocal?'api_hint_local':'api_hint_cloud');
}
function setProv(p){curProv=p;document.querySelectorAll('#provSeg button').forEach(b=>b.classList.toggle('active',b.dataset.prov===p));applyProvVisibility();}
document.querySelectorAll('#provSeg button').forEach(b=>b.onclick=()=>{setProv(b.dataset.prov);if(b.dataset.prov==='ollama'){if(!document.getElementById('apiUrl').value||document.getElementById('apiUrl').value.includes('groq'))document.getElementById('apiUrl').value='http://localhost:11434/v1/chat/completions';}else{if(document.getElementById('apiUrl').value.includes('localhost'))document.getElementById('apiUrl').value='https://api.groq.com/openai/v1/chat/completions';}});
function loadApiModal(){setProv(CFG.provider||'groq');document.getElementById('apiKey').value=CFG.api_key||'';document.getElementById('apiUrl').value=CFG.api_url||'';document.getElementById('modelName').value=CFG.model_name||'';document.getElementById('aiCallsignFilter').checked=(CFG.ai_callsign_filter==='on');}
document.getElementById('btnApiSave').onclick=async()=>{const key=(curProv==='groq')?document.getElementById('apiKey').value.trim():'';await api('save_api_config',curProv,key,document.getElementById('apiUrl').value.trim(),document.getElementById('modelName').value.trim());CFG.provider=curProv;CFG.api_key=key;CFG.api_url=document.getElementById('apiUrl').value.trim();CFG.model_name=document.getElementById('modelName').value.trim();const f=document.getElementById('aiCallsignFilter').checked?'on':'off';await api('save_field','ai_callsign_filter',f);CFG.ai_callsign_filter=f;closeModal('apiModal');toast(t('api_saved'));};

/* audio modal */
let capturing=false;
document.getElementById('btnCapKey').onclick=async function(){
  if(capturing)return;capturing=true;
  const b=this,i=document.getElementById('toggleKey');
  b.textContent=t('toggle_key_press');b.style.background='var(--amber)';b.style.color='#000';
  try{
    const r=await api('capture_hotkey',6.0);
    if(r&&r.combo)i.value=r.combo;
  }catch(e){}
  capturing=false;b.textContent=t('toggle_key_capture');b.style.background='';b.style.color='';
};
async function refreshDevices(){const s=document.getElementById('deviceSel');s.innerHTML=`<option>${t('searching')}</option>`;const r=await api('get_devices');if(!r||r.error){s.innerHTML='<option>'+t('no_devices')+'</option>';return;}const d=r.devices||[];if(!d.length){s.innerHTML='<option>'+t('no_devices')+'</option>';return;}s.innerHTML=d.map(x=>{const tag=x.is_default?' ★ (default)':'';return `<option value="${x.idx}" data-name="${esc(x.name)}" data-default="${x.is_default?1:0}">[${x.idx}] ${esc(x.name)}${tag}</option>`;}).join('');const last=r.last_device||'';let matched=false;if(last){for(const o of s.options){if(o.dataset.name===last){s.value=o.value;matched=true;break;}}}if(!matched){for(const o of s.options){if(o.dataset.default==='1'){s.value=o.value;break;}}}}
async function loadProcesses(){const s=document.getElementById('processSel'),h=document.getElementById('procCount');const r=await api('get_processes');if(!r)return;if(r.error){s.innerHTML=`<option value="">${t('audio_process_all')}</option>`;h.textContent=r.error;return;}const p=r.processes||[];s.innerHTML=`<option value="">${t('audio_process_all')}</option>`+p.map(x=>`<option value="${esc(x.name)}">${x.highlight?'* ':''}[${x.pid}] ${esc(x.name)}</option>`).join('');const last=r.last_process||'';if(last)for(const o of s.options)if(o.value===last){s.value=o.value;break;}h.textContent=t('n_processes',p.length);updateAudioSourceBanner();s.addEventListener('change',updateAudioSourceBanner);document.getElementById('deviceSel').addEventListener('change',updateAudioSourceBanner);}
function updateAudioSourceBanner(){const proc=document.getElementById('processSel').value,dev=document.getElementById('deviceSel').selectedOptions[0],b=document.getElementById('audioSrcBanner');if(!b)return;if(proc){b.innerHTML='&#127911; '+t('source_process')+': <b>'+esc(proc)+'</b>';b.className='hint hint-ok';}else{b.innerHTML='&#127911; '+t('source_device')+': <b>'+esc(dev&&dev.dataset.name||t('device_none'))+'</b>';b.className='hint';}}
document.getElementById('btnAudioSave').onclick=async()=>{const ds=document.getElementById('deviceSel'),o=ds.selectedOptions[0];if(o&&o.dataset.name){await api('save_field','last_device',o.dataset.name);CFG.last_device=o.dataset.name;}const tk=document.getElementById('toggleKey').value||'f9';await api('save_toggle_key',tk);CFG.toggle_key=tk;const proc=document.getElementById('processSel').value;await api('save_field','last_process',proc);CFG.last_process=proc;const wm=document.getElementById('whisperModel').value;await api('save_field','whisper_model',wm);CFG.whisper_model=wm;const ms=document.getElementById('micSensitivity').value;await api('save_field','mic_sensitivity',ms);CFG.mic_sensitivity=ms;refreshDeviceInfo();closeModal('audioModal');toast(t('audio_saved'));};

/* lang modal */
async function loadLangModal(){const r=await api('list_languages');const langs=(r&&r.languages)||[{code:'en',name:'English',complete:true}];
  const tag=l=>l.complete?esc(l.name):esc(l.name)+' \u26A0 '+t('lang_incomplete');
  const ui=document.getElementById('uiLangSel');ui.innerHTML=langs.map(l=>`<option value="${l.code}">${tag(l)}</option>`).join('');ui.value=CFG.ui_lang||'en';
  const v=document.getElementById('voiceLangSel');v.innerHTML=`<option value="auto">${t('lang_voice_auto')}</option>`+langs.map(l=>`<option value="${l.code}">${esc(l.name)}</option>`).join('');v.value=CFG.voice_lang||'auto';}
document.getElementById('btnLangSave').onclick=async()=>{
  const ui=document.getElementById('uiLangSel').value, v=document.getElementById('voiceLangSel').value;
  const uiChanged = ui !== (CFG.ui_lang||'en');
  await api('save_field','voice_lang',v); CFG.voice_lang=v;
  closeModal('langModal');
  if(!uiChanged){ toast(t('lang_saved')); return; }
  confirmDo(t('confirm_restart_lang'), async ()=>{
    await api('save_field','ui_lang',ui); CFG.ui_lang=ui;
    toast(t('restarting'));
    const r=await api('restart_app');
    if(!r || !r.restarted) setTimeout(()=>{ toast(t('restart_failed_reopen'),'error'); setTimeout(()=>api('close_app'),2200); }, 50);
  });
};


/* startup lang gear - only visible on startup screen */
document.getElementById('startLangGear').onclick=async()=>{openModal('langModal');await loadLangModal();};
function openPlanModal(){const p=(SNAP&&SNAP.plan)||{};document.getElementById('ep_callsign').value=p.callsign||'';document.getElementById('ep_tipo').value=p.tipo||'IFR';document.getElementById('ep_dep').value=p.dep||'';document.getElementById('ep_arr').value=p.arr||'';document.getElementById('ep_pista').value=p.pista_sid||'';document.getElementById('ep_notas').value=p.notas||'';openModal('planModal');}
document.getElementById('btnPlanSave').onclick=async()=>{const cs=document.getElementById('ep_callsign').value.trim(),tp=document.getElementById('ep_tipo').value,dep=document.getElementById('ep_dep').value.trim().toUpperCase(),arr=document.getElementById('ep_arr').value.trim().toUpperCase(),pi=document.getElementById('ep_pista').value.trim(),no=document.getElementById('ep_notas').value.trim();
  if(SNAP&&SNAP.plan){SNAP.plan.callsign=cs;SNAP.plan.tipo=tp;SNAP.plan.dep=dep;SNAP.plan.arr=arr;SNAP.plan.pista_sid=pi;SNAP.plan.notas=no;}
  await api('update_plan',cs,tp,dep,arr,pi,no);
  document.getElementById('flightTag').innerHTML=`<b>${esc(cs)}</b> ${esc(tp)}<br>${esc(dep||'?')} &rarr; ${esc(arr||'?')}`;closeModal('planModal');toast(t('plan_saved'));};

/* help tabs */
const HELP_KEYS=[["help_tab_overview","help_overview"],["help_tab_groq","help_groq"],["help_tab_apikey","help_apikey"],["help_tab_audio","help_audio"],["help_tab_ollama","help_ollama"],["help_tab_faq","help_faq"]];
async function initHelp(){const r=await api('get_repo_url');REPO=(r&&r.url)||"";
  const tabs=document.getElementById('helpTabs');tabs.innerHTML=HELP_KEYS.map((h,i)=>`<div class="htab${i===0?' active':''}" data-h="${h[1]}">${t(h[0])}</div>`).join('');
  tabs.querySelectorAll('.htab').forEach(tb=>tb.onclick=()=>{tabs.querySelectorAll('.htab').forEach(x=>x.classList.remove('active'));tb.classList.add('active');showHelp(tb.dataset.h);});
  showHelp('help_overview');}
function showHelp(key){const c=document.getElementById('helpContent');let html=STR[key]||'';
  if(key==='help_faq')html+=`<p style="margin-top:10px">${t('help_repo_q')}</p><span class="repolink" id="repoLink">${t('help_open_repo')} &rarr;</span>`;
  c.innerHTML=html;const rl=document.getElementById('repoLink');if(rl)rl.onclick=()=>{if(REPO)api('open_url',REPO)||window.open(REPO);};}

/* listen */
document.getElementById('btnStart').onclick=async()=>{const r=await api('get_devices');let idx=NaN;if(r&&r.devices&&r.devices.length){const last=CFG.last_device||r.last_device;let f=r.devices.find(d=>d.name===last);if(!f)f=r.devices.find(d=>d.is_default);if(!f)f=r.devices[0];idx=f.idx;if(f.name!==last){CFG.last_device=f.name;await api('save_field','last_device',f.name);refreshDeviceInfo();}}if(isNaN(idx)){toast(t('err_no_device'),'error');return;}const res=await api('start_listening',idx);if(res&&res.error)toast(transErr(res.error),'error');};
document.getElementById('btnStop').onclick=()=>api('stop_listening');
function transErr(e){if(e==='__err_no_api__')return t('err_no_api');if(e==='__err_invalid_device__')return t('err_invalid_device');return e;}

/* manual */
async function sendManual(){const i=document.getElementById('manualIn'),x=i.value.trim();if(!x)return;const q=document.getElementById('isQ').checked;i.value='';const r=await api('send_message',x,q);if(r&&r.error)toast(transErr(r.error),'error');}
document.getElementById('btnSend').onclick=sendManual;
document.getElementById('manualIn').addEventListener('keydown',e=>{if(e.key==='Enter')sendManual();});

/* status */
function setStatus(d){const el=document.getElementById('statusLine');el.className=d.color||'';let txt=d.key?t(d.key,d.arg):(d.text||'');el.innerHTML='<span class="dot"></span>'+esc(txt);}

/* polling */
function startPolling(){setInterval(async()=>{const ups=await api('poll_updates');if(!ups)return;for(const u of ups){
  if(u.type==='status')setStatus(u.data);
  else if(u.type==='transcript')document.getElementById('deviceInfo').textContent=t('last_transcript')+': '+u.data.text.slice(0,55);
  else if(u.type==='transcript_skip')document.getElementById('deviceInfo').textContent=t('skipped_other')+': '+u.data.text.slice(0,55);
  else if(u.type==='atc_message')onAtcMessage(u.data);
  else if(u.type==='instructor')showInstructor(u.data);
  else if(u.type==='listening'){document.getElementById('btnStart').disabled=u.data.active;document.getElementById('btnStop').disabled=!u.data.active;document.getElementById('vuRow').style.display=u.data.active?'flex':'none';}
  else if(u.type==='vu'){const f=document.getElementById('vuFill'),g=document.getElementById('vuGate');if(f){const pct=Math.min(100,Math.max(0,u.data.rms/12));f.style.width=pct+'%';if(g)g.style.left=Math.min(100,u.data.gate/12)+'%';}}
  else if(u.type==='skipped_changed'){const b=document.getElementById('skipBadge');if(b){if(u.data.count>0){b.textContent=u.data.count;b.style.display='flex';}else{b.style.display='none';}}}
  else if(u.type==='toast'){let m=u.data.key?t(u.data.key):(u.data.raw||'');toast(m,u.data.kind);}
}},250);}
function showInstructor(d){
  toast(t('toast_instructor'));selectTab('Extra');
  const tl=document.getElementById('tabLeft');if(!tl)return;
  const div=document.createElement('div');div.className='iblock';
  div.style.borderColor='var(--purple)';
  div.innerHTML=`<div class="iblock-hdr"><span class="iblock-n" style="color:var(--purple)">${esc(t('sec_instructor'))}</span><span class="iblock-t">${esc(new Date().toLocaleTimeString('en-GB'))}</span></div>
    <div class="iblock-sec"><div class="iblock-lbl">Q</div><div class="iblock-txt">${esc(d.question)}</div></div>
    <div class="iblock-sec"><div class="iblock-lbl">A</div><div class="iblock-txt">${esc(d.answer)}</div></div>`;
  tl.insertBefore(div,tl.firstChild);
}

/* boot */
let _init=false;
async function init(){if(_init)return;_init=true;await loadCfg();await setUiLang(CFG.ui_lang||'en');startPolling();}
window.addEventListener('pywebviewready',init);waitReady(init);
</script>
</body></html>"""


# ── MAIN ─────────────────────────────────────────────────────────────────────────
def _apply_window_icon(hwnd):
    """Sets the window/taskbar icon via WinAPI WM_SETICON — works regardless of
    pywebview version (older versions don't accept icon= in create_window)."""
    try:
        if not os.path.exists(ICON_PATH): return
        IMAGE_ICON = 1; LR_LOADFROMFILE = 0x10; LR_DEFAULTSIZE = 0x40
        WM_SETICON = 0x0080; ICON_SMALL = 0; ICON_BIG = 1
        h_small = ctypes.windll.user32.LoadImageW(None, ICON_PATH, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        h_big   = ctypes.windll.user32.LoadImageW(None, ICON_PATH, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
        if h_small: ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, h_small)
        if h_big:   ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, h_big)
        log.info("Window icon applied from %s", ICON_PATH)
    except Exception as e:
        log.warning("apply_window_icon: %s", e)

def _dark_titlebar_deferred(title):
    time.sleep(0.6)
    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if hwnd:
            apply_dark_titlebar(hwnd)
            _apply_window_icon(hwnd)
    except Exception as e:
        log.warning("titlebar: %s", e)

if __name__ == "__main__":
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ivao.vatsim.atc.assistant")
    except Exception: pass
    try:
        is_restart = "--restart" in sys.argv
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\IVAO_VATSIM_ATC")
        if is_restart:
            # The old instance may still be shutting down (mutex releases only on
            # process exit). Retry briefly instead of immediately treating this as
            # "already running" and stealing focus from a window that's closing.
            tries = 0
            while ctypes.windll.kernel32.GetLastError() == 183 and tries < 25:
                try: ctypes.windll.kernel32.CloseHandle(mutex)
                except Exception: pass
                time.sleep(0.2); tries += 1
                mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\IVAO_VATSIM_ATC")
        if ctypes.windll.kernel32.GetLastError() == 183:
            hwnd = ctypes.windll.user32.FindWindowW(None, "ATC Assistant")
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 9); ctypes.windll.user32.SetForegroundWindow(hwnd)
            sys.exit(0)
    except Exception as e:
        log.warning("mutex: %s", e)

    api = Api()
    # Wire global toggle hotkey: pressing the configured key starts/stops listening
    # without needing the window to be focused.
    _S.hotkey.start(lambda: api.toggle_listening())
    threading.Thread(target=_dark_titlebar_deferred, args=("ATC Assistant",), daemon=True).start()
    try:
        try:
            # Newer pywebview versions accept icon=; older ones don't.
            window = webview.create_window(title="ATC Assistant", html=HTML, js_api=api,
                icon=ICON_PATH if os.path.exists(ICON_PATH) else None,
                width=900, height=820, min_size=(720, 640),
                background_color="#101113", text_select=False)
        except TypeError:
            window = webview.create_window(title="ATC Assistant", html=HTML, js_api=api,
                width=900, height=820, min_size=(720, 640),
                background_color="#101113", text_select=False)
        _S.window = window; log.info("Window created")
    except Exception as e:
        log.critical("create_window: %s", e, exc_info=True); raise
    try:
        webview.start(debug=False, private_mode=False)
    except Exception as e:
        log.critical("webview.start: %s", e, exc_info=True); raise
