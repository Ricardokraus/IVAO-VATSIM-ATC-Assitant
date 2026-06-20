<div align="center">

# IVAO/VATSIM ATC Assistant

**AI-powered ATC assistant for virtual pilots on IVAO and VATSIM.**

Listens to ATC audio, transcribes it with Whisper locally, asks an LLM to interpret the instruction, and shows you a structured readback you can read straight to the frequency.

[![Status](https://img.shields.io/badge/status-alpha-orange)](#project-status)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey)](#requirements)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</div>

---

> ## 🚧 Work in progress
>
> **This app is in active development and is not polished.** Expect rough edges, breaking changes, missing features, occasional crashes, and unfinished translations. There are **no official releases yet** — you run it directly from the Python source on Windows.
>
> Bug reports and PRs are very welcome ([Issues](https://github.com/Ricardokraus/IVAO-VATSIM-ATC-Assitant/issues) · [Discussions](https://github.com/Ricardokraus/IVAO-VATSIM-ATC-Assitant/discussions)).

---

## Table of contents

- [What it does](#what-it-does)
- [Project status](#project-status)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuring an AI provider](#configuring-an-ai-provider)
  - [Groq (cloud, free tier)](#groq-cloud-free-tier)
  - [Ollama (local, CPU or GPU)](#ollama-local-cpu-or-gpu)
- [Whisper models](#whisper-models)
- [How the audio pipeline works](#how-the-audio-pipeline-works)
- [Project layout](#project-layout)
- [Persistence and data files](#persistence-and-data-files)
- [Internationalization](#internationalization)
- [Building a single `.exe`](#building-a-single-exe)
- [Known limitations](#known-limitations)
- [Contributing](#contributing)
- [License and disclaimer](#license-and-disclaimer)

---

## What it does

For every ATC transmission the app captures, you get:

| Output | What it is |
|---|---|
| **Literal transcript** | Whisper's verbatim text of what ATC said. |
| **Structured quick list** | Squawk, runway, frequencies, headings, levels… in standard aviation abbreviations. |
| **Pilot readback** | Ready-to-read response in the controller's language, ending with your callsign. |
| **Suggested next callout** | The next thing you should typically initiate, when applicable. |
| **Live global panel** | All current clearances (last value wins) — editable by hand. |
| **Personal notes** | Stored per-flight, never sent to the AI. |

Every transmission, readback and edit is logged to a JSON file you can replay later from **Flight → Load flight**.

---

## Project status

| Aspect | State |
|---|---|
| Stability | **Alpha.** Crashes happen. |
| Releases | None yet. Run from source. |
| Platform | Windows only (WASAPI loopback). macOS/Linux would need a separate audio path. |
| Languages | 15 UI languages; quality varies. EN/ES/FR/IT/PT are reviewed; others are machine-assisted. |
| Roadmap | See [open issues](https://github.com/Ricardokraus/IVAO-VATSIM-ATC-Assitant/issues). |

---

## Requirements

- **Windows 10 or 11** (WASAPI loopback is Windows-only).
- **Python 3.10 or newer** (tested up to 3.14).
- One of:
  - A **Groq** API key (free tier, [console.groq.com/keys](https://console.groq.com/keys)), or
  - A local **[Ollama](https://ollama.com/)** install, or
  - Any other OpenAI-compatible endpoint (OpenRouter, LM Studio, etc.).
- **Microphone or audio output device** Windows can route ATC audio through.

---

## Quick start

```powershell
# 1. Clone the repo
git clone https://github.com/Ricardokraus/IVAO-VATSIM-ATC-Assitant.git
cd IVAO-VATSIM-ATC-Assitant

# 2. (Recommended) create a virtual environment
python -m venv .venv
.\.venv\Scripts\activate

# 3. Install the dependencies
pip install -r requirements.txt

# 4. Run it
python atc_assistant.pyw
```

On first launch:

1. Open **Settings → API & Connection** and configure your AI provider (see below).
2. Open **Settings → Audio & Modes** and pick the audio device or process.
3. Click **New Flight**, enter your callsign and route, and **Save**.
4. Hit **Start** and fly.

> **Don't double-click `atc_assistant.pyw` directly.** Windows runs `.pyw` files
> with whatever Python is associated with that extension system-wide — which is
> usually **not** your venv, and will silently fail (no window, no error, often
> no log line at all) if the global Python doesn't have the dependencies
> installed. Use **`run.bat`** instead (always launches with this project's
> `.venv`), or **`run_debug.bat`** if something's wrong and you need to see the
> actual error in a console window.

---

## Configuring an AI provider

### Groq (cloud, free tier)

| Field | Value |
|---|---|
| Provider | `Groq` |
| API URL | `https://api.groq.com/openai/v1/chat/completions` |
| API key | Paste your key from [console.groq.com/keys](https://console.groq.com/keys) |
| Model | `llama-3.1-8b-instant` (default) or `llama-3.3-70b-versatile` for better accuracy |

The key is stored in your OS keyring (Windows Credential Manager) when the optional `keyring` package is installed.

### Ollama (local, CPU or GPU)

Install Ollama from [ollama.com](https://ollama.com/), then pull a model:

```powershell
ollama pull qwen2.5:7b-instruct
```

In the app, set:

| Field | Value |
|---|---|
| Provider | `Ollama` |
| API URL | `http://localhost:11434/v1/chat/completions` (the app auto-routes to the native `/api/chat` endpoint internally) |
| Model | `qwen2.5:7b-instruct`, `llama3.1:8b`, `mistral:7b-instruct`, etc. |

**CPU-only mode** is forced automatically when the Ollama provider is selected (via `options.num_gpu=0` on the native endpoint). This keeps your GPU fully available for your flight simulator. On a 7B model in int4/int8 you should see ~5–10 GB of RAM and ~50–70% CPU during inference.

If you want to confirm CPU usage:

```powershell
ollama ps
```

The `PROCESSOR` column should show `100% CPU`.

---

## Whisper models

Select in **Settings → Audio & Modes → Whisper model**:

| Model | Size on disk | Use case |
|---|---|---|
| `tiny` | ~75 MB | Very fast, low accuracy. Casual use. |
| `base` | ~145 MB | Step up from tiny. |
| `small` | ~245 MB | **Recommended.** Best speed/accuracy balance on CPU. |
| `medium` | ~1.5 GB | Higher accuracy, ~3× slower than small on CPU. |
| `ATC fine-tuned (English only)` | ~1.5 GB | Whisper medium fine-tuned on ATCO2 + UWB-ATCC ATC datasets. ~84% lower WER on real ATC audio. **English transmissions only.** Auto-downloaded from Hugging Face on first use. |
| `ATC auto` | varies | Uses the ATC fine-tuned model when `voice_lang=en`, otherwise falls back to `small`. |

The model auto-unloads from RAM 60 seconds after **Stop**, freeing memory while you're paused.

Credit for the ATC fine-tuned model: [jack-tol/fine-tuning-whisper-on-atc-data](https://github.com/jack-tol/fine-tuning-whisper-on-atc-data).

---

## How the audio pipeline works

```
Audio in → Whisper (local) → Local callsign filter → LLM (cloud/local) → UI
                                        ↓                    ↓
                            drop if not for us       drop if "Ignore"
```

Three discard stages, all logged to `data/discarded_transcripts.log` so nothing is lost:

1. **Whisper garbage filter** — drops empty output or known hallucinations ("Thanks for watching", "Subtítulos", silence artifacts).
2. **Local callsign filter** — fast regex pass that drops transmissions clearly directed at other aircraft, saving ~70% of LLM tokens on a busy frequency.
3. **AI Ignore** — the LLM can still return `assigned_phase: "Ignore"` for ambiguous cases.

For custom callsigns or unusual phonetics, use the **Notes / aliases** field when you create a flight (e.g. `Air Lince = ALI`). It's sent to the AI as context and used by the local filter.

---

## Project layout

```
atc_assistant.pyw         ← the app (single Python file, pywebview UI)
requirements.txt
assets/
  └── Logo-ATC.ico
lang/
  ├── en.json, es.json, fr.json, it.json, pt.json    ← reviewed
  └── de, el, ja, nl, pl, ru, sv, th, tr, zh         ← machine-assisted (functional)
data/                     ← created automatically on first run
  ├── app.log                          ← rotating, 5 MB × 3 backups
  ├── discarded_transcripts.log        ← every discarded transcript with full text
  ├── config.txt                       ← UI settings
  └── Flights/
      └── IBE1234_20260618_2154.json   ← one file per flight
```

Nothing critical is written outside `data/` and `assets/`.

---

## Persistence and data files

- **Config and flight JSONs are written atomically** (temp file + `os.replace`) so an app crash or power loss mid-save cannot corrupt them.
- **`app.log`** is in append mode with rotation (5 MB × 3 backups). Crash diagnostics survive across restarts.
- **`discarded_transcripts.log`** stores the full text of every transcript that was filtered out, with the reason (`garbage`, `local_filter_no_callsign`, `ai_ignore`). Useful when you suspect the filter is too aggressive or when you need to recover something the app dropped after a crash.
- **API keys** are stored in the Windows Credential Manager via `keyring` (when installed).

---

## Internationalization

15 languages ship with the app. To improve a translation or add a new one, see [`lang/README.md`](lang/README.md).

---

## Building a single `.exe`

```powershell
pip install pyinstaller
pyinstaller --onefile --windowed --icon=assets/Logo-ATC.ico ^
  --add-data "lang;lang" --add-data "assets;assets" ^
  atc_assistant.pyw
```

The resulting `dist/atc_assistant.exe` runs standalone, no Python needed. `data/` is created next to the binary on first launch.

---

## Known limitations

- **Windows only.** WASAPI loopback is the capture path.
- **First Whisper load** downloads the model from Hugging Face (~250 MB for `small`, ~1.5 GB for `medium` / ATC). Plan for it on first run.
- **Local callsign filter false negatives** exist when ATC drastically abbreviates ("Iberia, climb…" with no number). The LLM handles these via `assigned_phase: "Ignore"`.
- **Audio routers like FXSound** can break WASAPI loopback by intercepting audio before it reaches the device endpoint. If the VU meter stays at 0, close any audio router and re-test.

---

## Contributing

Issues and PRs are very welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License and disclaimer

[MIT](LICENSE).

This tool is for **flight simulation only** (IVAO / VATSIM / single-player). It is **not** for use with real-world air traffic control. The author is not affiliated with IVAO, VATSIM, or any aviation authority.
