# Changelog

All notable changes to this project. The format is loosely based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Project is in alpha; versions are not yet pinned. Entries below are grouped by date.

## 2026-06-20

### Added
- **ATC fine-tuned Whisper model** as a selectable Whisper option (`atc-en` and `atc-auto`). Uses
  [jack-tol/whisper-medium.en-fine-tuned-for-ATC-faster-whisper](https://huggingface.co/jacktol/whisper-medium.en-fine-tuned-for-ATC-faster-whisper),
  ~84% lower WER than vanilla Whisper on real ATC audio. English only.
- **Real-time VU meter**: RMS is now computed per audio chunk (~12 Hz update) instead of once
  every 4 s, so the bar reflects the actual sound level continuously.
- **`data/discarded_transcripts.log`** — rotating log that persists every discarded transcript
  (garbage / callsign filter / AI Ignore) with full text and reason, so nothing is lost across
  app crashes.
- **Atomic file writes** (`tempfile + os.replace`) for `config.txt` and flight JSONs. A crash or
  power loss mid-write can no longer corrupt these files.
- **`requirements.txt`** for `pip install -r`.
- **MIT LICENSE**, **CONTRIBUTING.md**, **CHANGELOG.md**.

### Fixed
- Range slider visual glitch caused by a CSS specificity conflict with the generic `.mf input` field styling.
- `run.bat` / `run_debug.bat` launchers to bypass Windows' `.pyw` file association pointing at the wrong Python.
- Friendly, translated error messages when the AI provider (Ollama/Groq) is unreachable, instead of a raw traceback in the toast.
- WASAPI loopback device picker now marks and prefers the system default output.

### Changed
- **`app.log` rotates** (5 MB × 3 backups) and runs in **append mode**, so the log from a crash
  survives the next launch instead of being truncated.
- **Ollama provider** now routes to the native `/api/chat` endpoint instead of
  `/v1/chat/completions`. This is what allows `options.num_gpu=0` to actually force CPU-only
  inference — the OpenAI-compatible endpoint silently ignored it.
- **WASAPI loopback device picker** marks the system default output with ★, and `start_listening`
  falls back to the default loopback (not just `devices[0]`) when the saved device is missing.
  `open_capture_stream` tries multiple sample rates (48 kHz → 44.1 → 32 → 22 → 16) and mono
  fallback before giving up.
- **Capture loop diagnostics** every 10 s in `app.log`: peak RMS, gate, chunks pushed. A
  persistent peak under 1.0 logs a clear warning that the loopback is silent.
- **All 13 non-EN/ES UI languages** got the new Whisper option labels (`lang_whisper_atc_en` /
  `lang_whisper_atc_auto`) translated, so they no longer show the raw key.

### Removed
- Dead `input_process_id` / `input_process_loopback_mode` parameters that pyaudiowpatch never
  supported. The fallback path remains.

## Unreleased