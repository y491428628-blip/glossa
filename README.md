# Glossa

**A shared glossary and translation memory for teams that read in other languages.**

Glossa sits between your team's [Immersive Translate](https://immersivetranslate.com) browser extension and your own AI provider:

- **Team glossary.** Add "census date → 退课截止日" once. Everyone's translations use it from then on, in every page they open.
- **Shared memory.** When one teammate has translated a paragraph, everyone else gets it instantly from memory instead of paying the AI provider again.
- **Bring your own AI.** Works with any OpenAI-compatible API (OpenAI, DeepSeek, Gemini, OpenRouter, Groq and more). Your key stays on your server.
- **Dashboard.** Manage the glossary, let the AI suggest terms from a sample, try translations side by side, and see how much the memory has saved.

Nobody has to change how they read: teammates keep using Immersive Translate and only change one setting.

## Deploy on Zenith

Deploy Glossa from the Zenith catalogue. When it is running:

1. Open your deployment's address and sign in with the **Admin password** shown in the deployment's environment settings (`ADMIN_PASSWORD`).
2. Go to **Settings**, choose your AI provider, paste its API key and click **Test connection**.
3. Go to **Overview** and click **Copy setup message for your team**. Paste it in your group chat.

## Connect Immersive Translate

Each teammate does this once:

1. Immersive Translate settings → **Translation services** (翻译服务) → **OpenAI** → turn on **Custom API Key** (自定义 API Key).
2. **API address** (自定义 API 接口地址): `https://<your-glossa-address>/v1/chat/completions`
3. **API key**: the team key from the Glossa overview.
4. Choose OpenAI as the translation service.

Alternatively, enable Beta features in Immersive Translate's developer settings and add a **Custom API** (自定义 API) service with
`https://<your-glossa-address>/imme?token=<team key>`. This route caches paragraph by paragraph, so memory works even when pages only share a few sentences.

## How it works

```
Immersive Translate ──► Glossa ──► your AI provider
                          │
                          ├─ glossary: matching terms are added to the prompt as mandatory translations
                          ├─ memory:   cache keyed on text + language + model + matching glossary entries
                          └─ stats:    paragraphs, memory hits, glossary compliance, estimated savings
```

Editing a glossary term changes the cache key for exactly the paragraphs that contain it, so those are re-translated with the new term while everything else keeps coming from memory.

## API

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `POST /v1/chat/completions` | `Authorization: Bearer <team key>` | OpenAI-compatible proxy with glossary injection and caching (`stream` supported) |
| `POST /imme` | `?token=<team key>` or bearer | Immersive Translate custom API: `{source_lang, target_lang, text_list}` → `{translations: [{detected_source_lang, text}]}` |
| `GET /v1/models` | team key | Lists the configured model |
| `GET /healthz` | none | Health check |
| `/api/*` | admin session | Dashboard API (glossary, settings, stats, try-it) |

## Configuration

Settings saved in the dashboard take priority. Environment variables provide the defaults:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ADMIN_PASSWORD` | none | Dashboard password. If unset, the first visit asks you to create one. |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API address |
| `LLM_MODEL` | `gpt-4.1-mini` | Model name |
| `LLM_API_KEY` | none | Provider API key |
| `PUBLIC_URL` | request host | Address shown in the setup instructions |
| `DATA_DIR` | `/data` | Where the SQLite database lives |

## Run locally

```bash
pip install -r requirements.txt
DATA_DIR=./data uvicorn app.main:create_app --factory --port 8080
```

To try it without an API key, start the bundled mock provider and point Settings at `http://127.0.0.1:9999/v1` with the key `sk-test`:

```bash
uvicorn tests.mock_llm:app --port 9999
```

Run the tests with `pip install pytest && pytest`.

## Publish your own copy to Zenith

```bash
npm install -g @zenith-hosting/cli
zenith auth login
zenith check --upload-size
zenith up          # temporary test deployment
zenith publish     # after pushing to a public GitHub repo
```

Set `x-zenith.catalog.source` in `zenith-compose.yml` to your repository URL first.

## License

MIT. Fonts: Onest and Literata, SIL Open Font License (see `app/static/fonts`).
