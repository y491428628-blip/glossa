# Glossa

**A shared glossary and translation memory for teams that read in other languages.**

Glossa sits between your team's [Immersive Translate](https://immersivetranslate.com) browser extension and your own AI provider:

- **Team glossary.** Add "census date → 退课截止日" once. Everyone's translations use it from then on, in every page they open.
- **Shared memory.** When one teammate has translated a paragraph, everyone else gets it instantly from memory instead of paying the AI provider again.
- **Bring your own AI.** Works with any OpenAI-compatible API (OpenAI, DeepSeek, Gemini, OpenRouter, Groq and more). Your key stays on your server.
- **Stays under your rate limit.** Paragraphs arriving together are merged into a few AI calls, calls are queued under a requests-per-minute limit you set, and rate-limit or outage errors are retried automatically.
- **Safe to edit.** Very short, everyday or very broad terms need a second click, with examples of what they would change. Every change is in a history with one-click undo. A term can be turned off for a while without deleting it. Terms with several meanings can be marked *depends on meaning*: the note tells the AI when the translation applies, and it is never forced.
- **Different groups, different meanings.** Put terms into sets (General, Statistics, …) and give each group of teammates its own link that uses the sets it needs. "census date" can be 退课截止日 for one group and 人口普查日 for another, both strictly enforced.
- **Teammates can suggest terms.** A page that only needs the team key shows the glossary, lets teammates try a sentence, and takes suggestions to add, change or remove a term; the admin approves each one before it is used.
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

Every paragraph is cached on its own, keyed on text + language + model + the glossary entries it matches. Editing a term (or its note) re-translates exactly the paragraphs that contain it; plural, possessive, hyphenated and no-break-space forms of a term are matched. Term notes are sent to the AI with the term. Prompts that are not in Immersive Translate's paragraph format are forwarded whole, with the glossary added to the system prompt.

## Sets and links

Terms belong to a glossary set; every existing term starts in the built-in **General** set. Under **Sets & links** the admin creates sets and links. A link is an address handed to a group of teammates, and decides which sets their translations use:

- the default link is the plain `https://<your-glossa-address>/v1/chat/completions`;
- other links look like `https://<your-glossa-address>/g/<id>/v1/chat/completions` (and `/g/<id>/imme`), each with its own teammate page and setup message.

When two sets in a link contain the same term, the set higher in the list wins; General is always last. Paragraphs that contain no term that differs between links share the same memory.

## Teammate suggestions

Overview → **Suggestions from teammates** has a link of the form `https://<your-glossa-address>/suggest#key=<team key>` (the setup message includes it too). Teammates can browse the glossary there, try a sentence, and suggest a new term, a better translation or removing a term. Suggestions wait under **Glossary** until the admin approves or rejects them.

## API

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `POST /v1/chat/completions` | `Authorization: Bearer <team key>` | OpenAI-compatible proxy with glossary injection and caching (`stream` supported) |
| `POST /imme` | `?token=<team key>` or bearer | Immersive Translate custom API: `{source_lang, target_lang, text_list}` → `{translations: [{detected_source_lang, text}]}` |
| `GET /v1/models` | team key | Lists the configured model |
| `/g/<id>/v1/chat/completions`, `/g/<id>/imme`, `/g/<id>/v1/models` | team key | The same endpoints for a link made under Sets & links |
| `GET /api/team/glossary?link=<id>` | team key | The terms a link uses, read-only, for the teammate page |
| `POST /api/team/proposals` | team key | Suggest a term `{source, target, target_lang, note, name}`, or its removal `{kind: "remove", source, target_lang, note, name}` (30 per hour per address) |
| `POST /api/team/translate` | team key | Try it for teammates: `{text, target_lang}`, up to 5,000 characters |
| `GET /healthz` | none | Health check |
| `/api/*` | admin session | Dashboard API (glossary, sets and links, history and undo, proposals, settings, stats, try-it) |

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
