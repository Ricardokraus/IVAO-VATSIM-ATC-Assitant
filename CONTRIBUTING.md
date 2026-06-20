# Contributing

Thanks for considering contributing! This project is in alpha — every report and PR genuinely helps.

## Reporting bugs

Open an [issue](https://github.com/Ricardokraus/IVAO-VATSIM-ATC-Assitant/issues) and include:

- What you were doing when it happened (network, callsign, frequency).
- The relevant lines from `data/app.log` (it persists between sessions thanks to log rotation).
- If a transcription seems wrong, the relevant entry from `data/discarded_transcripts.log` is very useful.
- OS version, Python version (`python --version`), and the model you were using (Whisper + LLM).

Please scrub any API keys before pasting.

## Suggesting features

Open a [discussion](https://github.com/Ricardokraus/IVAO-VATSIM-ATC-Assitant/discussions) — easier to iterate on the idea before turning it into an issue or PR.

## Code contributions

- The whole app is a single file (`atc_assistant.pyw`). That's intentional for now — keep it that way until the project warrants splitting.
- Match the existing style: terse, comment only where the *why* isn't obvious, no over-formatting.
- The HTML/CSS/JS frontend lives in a heredoc at the bottom of the file. Keep it self-contained.
- For Python: prefer the standard library. New runtime dependencies need a clear justification.

Before opening a PR:

```powershell
python -m py_compile atc_assistant.pyw
```

If the change affects user-visible strings, also update at minimum `lang/en.json` and `lang/es.json`. Other languages can stay machine-assisted.

## Translation contributions

See [`lang/README.md`](lang/README.md).

## Commit messages

Short, imperative, lowercase first word: `fix wasapi loopback fallback`, `add discarded transcripts log`, `bump faster-whisper to 1.1`.

## License

By contributing you agree your code will be released under the project's [MIT license](LICENSE).
