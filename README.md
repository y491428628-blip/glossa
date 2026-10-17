# Glossa

**A shared glossary and translation memory for teams that read in other languages.**

Glossa sits between your team's [Immersive Translate](https://immersivetranslate.com) browser extension and your own AI provider:

- **Team glossary.** Add "census date → 退课截止日" once. Everyone's translations use it from then on, in every page they open.
- **Shared memory.** When one teammate has translated a paragraph, everyone else gets it instantly from memory instead of paying the AI provider again.
- **Bring your own AI.** Works with any OpenAI-compatible API (OpenAI, DeepSeek, Gemini, OpenRouter, Groq and more). Your key stays on your server.
- **Stays under your rate limit.** Paragraphs arriving together are merged into a few AI calls, calls are queued under a requests-per-minute limit you set, and rate-limit or outage errors are retried automatically.
- **Dashboard in six languages.** English, 简体中文, 繁體中文, 日本語, 한국어 and Tiếng Việt. Manage the glossary, let the AI suggest terms from a sample, try translations side by side, and see how much the memory has saved.

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
`https://<your-glossa-address>/imme?token=<team key>`. Both routes share the same glossary and memory.

## How it works

```
Immersive Translate ──► Glossa ──────────────────────────────────────────► your AI provider
                          split [[pN]] paragraphs
                          ├─ exact glossary term?  answer directly, no AI call
                          ├─ in shared memory?     answer from memory
                          └─ otherwise             dispatcher: merge with other pending paragraphs,
                                                   wait for a slot under the per-minute limit,
                                                   retry 429/5xx, check glossary terms were used
```

Every paragraph is cached on its own, keyed on text + language + model + the glossary entries it matches. Editing a term re-translates exactly the paragraphs that contain it; plural, possessive, hyphenated and no-break-space forms of a term are matched. Prompts that are not in Immersive Translate's paragraph format are forwarded whole, with the glossary added to the system prompt.

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
| `MAX_RPM` | `12` | Requests per minute Glossa sends to the provider; `0` for no limit |
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

The mock can also simulate rate limits: `curl -X POST "localhost:9999/_fail?count=3&status=429"`.

Run the tests with `pip install pytest && pytest`. Known issues and their fixes are tracked in `ISSUES.md`.

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
