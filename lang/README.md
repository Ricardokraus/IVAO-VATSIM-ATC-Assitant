# Translations

Each `.json` file here is one interface language. The app loads them automatically and shows every available language in the language selector (start screen gear icon, or **Settings → Language**).

## Status

- **Complete & reviewed:** `en`, `es`, `fr`, `it`, `pt`
- **Templates (English placeholder text, need translation):** `de`, `ru`, `zh`, `ja`, `th`, `el`, `nl`, `pl`, `sv`, `tr`

Files marked `"author": "TEMPLATE - needs translation"` in their `_meta` block still contain English values and are waiting for a translator.

## How to add or improve a language

1. Copy `en.json` to `xx.json`, where `xx` is the [ISO 639-1 code](https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes) (e.g. `ko.json` for Korean).
2. Edit the `_meta` block:
   ```json
   "_meta": { "name": "한국어", "code": "ko", "voice": "ko", "author": "your-name" }
   ```
   - `name`: the language's name in its own script (shown in the selector).
   - `code`: the file/language code.
   - `voice`: the Whisper language code for speech recognition (usually the same).
3. Translate every **value**. **Do not change the keys** (the part before the `:`).
4. Keep placeholders like `{0}` intact — they are replaced at runtime (e.g. counts, model names).
5. Strings starting with `help_` may contain simple HTML (`<p>`, `<b>`, `<span class='key'>`). Keep the tags, translate the text.
6. Drop the file in this folder and restart the app. It appears in the selector automatically.

## Notes

- The interface language controls what you see (menus, buttons, the AI's instruction summary and quick list).
- The **readback** is always kept in the language the controller actually spoke, regardless of interface language — that's real ATC phraseology.
- Pull requests with new or improved translations are very welcome.
