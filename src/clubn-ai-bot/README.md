# ClubÑ AI Bot

A Discord bot for ClubÑ that answers members' questions with a **local** language model running on
the AMP VM's CPU. Every question is checked against the server rules first; answers are built only
from retrieved sources and cite them. When the sources don't support an answer, the bot says so
instead of guessing.

Everything is configured in the AMP instance settings. Nothing is hard-coded and no secret is stored
in this repository.

## Club Ñ web features (formerly SuperAntonia)

With **Club Ñ Web Features** ticked in the AMP settings, the same bot process also runs everything
SuperAntonia did, on the same Discord connection:

- **Creator articles:** a member gets the creator role, the bot collects their socials, drafts a
  WordPress article and posts a card in the mod channel (✅ publish, ✏️ change, 🗑️ discard). Daily sweep at 9:00.
- **Events calendar:** Discord announcements and scheduled events become draft calendar entries and
  articles in *Eventos*; every update needs a moderator's ✅.
- **Momentazos page** and **Ahora en directo** (Twitch/YouTube live) page updates. No AI.
- **/mis_redes:** creators propose changes to their own socials in the creator database; a moderator approves.
- Mod commands: `/redes-creador`, `/generar-articulo`, `/evento-desde-mensaje`, `/evento-actualizar`,
  `/eventos`, `/directo-creador`, `/directo-quitar`, `/directos-importar`, `/directos`.

**AI for drafts:** Claude first, then DeepSeek, then this bot's local model as the last resort. A
provider that answers "no credits" or "bad key" is skipped for 6 hours; any other error moves on to
the next one for that draft only. Local drafts share the CPU queue with questions. Nothing is
published without a moderator.

**Settings:** all of them are in AMP, under **Configuration > Club Ñ Web** (secrets are password
fields): WordPress user and application password, Claude/DeepSeek/YouTube/Twitch keys, channels,
roles, page ids and one switch per feature. Optionally, a blank AMP field can be filled from
`/AMP/clubn-ai-bot/.env` (template: `.env.example`, copied there by Update). Secrets are never in
the repository, and Ollama and the model download never see them.

**Discord:** run it with the token of the bot that owns the creator database (SuperAntonia): the
live checker only trusts database messages written by itself. Turn on the **Server Members** and
**Message Content** intents. The question command (Slash Command Name, e.g. `fulgencio`) replaces
SuperAntonia's old `/fulgencio` placeholder. Only one copy may run at a time: stop the old
SuperAntonia container before starting this, or every draft and card is made twice.

**State:** SQLite at `/AMP/clubn-ai-bot/data/clubn_web.db`. To keep the history (articles in review,
followed events, Momentazos seen, /mis_redes audit), copy `bot.db` from SuperAntonia's
`creator-bot-data` volume to that path before the first start.

**Tools** (from the instance console or a shell in the container):
```
cd /AMP/clubn-ai-bot/app
../venv/bin/python -m clubn_bot web momentazos            # dry run; add --apply to update the page
../venv/bin/python -m clubn_bot web live                  # who is live now
../venv/bin/python -m clubn_bot web mis_redes --audit
```

**Maintenance:** `clubn_web/` is an unmodified copy of SuperAntonia's `bot/` package;
refresh it with `python tools/sync_clubn_web.py <path to that bot/ folder>`. The wiring lives in `clubn_bot/merged.py` and
`clubn_bot/ai_chain.py`. New settings go in `clubn_bot/web_settings.py`, then
`python tools/gen_amp_settings.py` regenerates the AMP files and `defaults/env.example`; the tests
fail if a `clubn_web` setting is missing from AMP.

## How a question flows

1. A member runs `/pregunta` (the name is configurable) in an allowed channel.
2. **Cheap checks:** allowed channel and role, per-user cooldown and daily limit, length, `@everyone`
   and invites, known prompt-injection phrases, and your own blocked patterns. A plain greeting
   ("hola", "hello") gets a short fixed reply without using the model.
3. **Rules check by the model:** the rules-check model judges the question against `rules.md` plus the AMP
   "Extra Server Rules" field and replies with a single label (`ok` or a refusal category). Anything
   other than exactly one known label, and any model error, is a refusal (fail closed). Refusals are shown only to the asker, and optionally
   reported in a staff channel.
4. **Retrieval:** the server's own knowledge files first, then Wikipedia (es, en), then web search
   (DuckDuckGo or your own SearXNG). The top pages are downloaded with an SSRF guard (public IPs only,
   ports 80/443, size cap, redirect checks).
5. **Grounded answer:** the model may only use the numbered sources and must cite them as `[1]`.
   Citations to sources that don't exist are removed, and an answer with no valid citation is
   discarded as speculation.
6. **Output check:** a second model pass reviews the answer (catches instructions smuggled in through
   web pages). Mentions, invites and stray links are stripped; Discord pings are disabled.
7. The answer is posted publicly with a "Fuentes:" list. Every decision is appended to
   `logs/audit.jsonl`.

## Model choice: two Liquid AI models

The bot uses two models, both from Liquid AI, both on CPU through Ollama:

- **Answers: [LFM2.5-24B-A2B](https://docs.liquid.ai/lfm/models/complete-library)** at `Q4_K_M`
  (`hf.co/LiquidAI/LFM2.5-24B-A2B-GGUF:Q4_K_M`). It is a mixture-of-experts model: about 24B
  parameters in total but only about 2B active per token, so it writes at roughly the speed of a 2B
  model while knowing and reasoning far more. It needs about 15 GB of RAM (my estimate for Q4_K_M,
  not measured). This is the setting for a server with 16 GB or more, such as 31 GB on a 6-core
  i5-12500T.
- **Rules checks: [LFM3-1.7B](https://docs.liquid.ai/lfm/models/lfm3-1.7b)** at `Q4_0`
  (`hf.co/LiquidAI/LFM3-1.7B-GGUF:Q4_0`, about 1 GB). The checks reply with one word, so the small
  fast model keeps the waiting time down.

On a machine with 8 GB of RAM, set Answer Model to `hf.co/LiquidAI/LFM3-1.7B-GGUF:Q4_0` too. Answers
will be noticeably weaker: Liquid AI says LFM3-1.7B is not meant for complex multi-step reasoning.

- Needs Ollama 0.40.13 or newer on Linux (the image pins 0.40.13).
- Reasoning ("thinking") is turned off for speed on CPU. Switch "Reasoning Mode" to On if answers seem
  shallow and you can accept slower replies. Models without a reasoning mode simply ignore it.
- Liquid AI's license (LFM Open License v1.0) is free for organisations under US$10M annual revenue.
- Whatever the model, the bot never lets it answer from memory: it summarises retrieved sources and
  cites them.

Both models stay loaded for the "Unload Model After Idle" time (default 30 minutes) and load again on
the next question, which adds a few seconds to that answer (longer for the 15 GB answer model on a
slow disk). The first Update downloads both models, about 16 GB in total.

## Install in AMP

1. **Publish the image.** Merging to `main` runs `.github/workflows/publish-clubn-ai-bot-image.yml`,
   which builds `ghcr.io/cjguirao/amp-clubn-ai-bot:latest` from `cubecoders/ampbase:debian` with a
   CPU-only Ollama. Make the package public in GitHub (Packages > amp-clubn-ai-bot > Package settings)
   or give the AMP host a pull credential.
2. **Create the Discord application.** In the Developer Portal: New Application > Bot > Reset Token
   (copy it). Leave the privileged intents off unless you want "Answer When Mentioned". Invite it with
   OAuth2 > URL Generator, scopes `bot` and `applications.commands`, permissions **View Channels**,
   **Send Messages**, **Read Message History**, **Add Reactions**. Nothing else.
3. In AMP, **Configuration > Instance Deployment**, fetch this repository
   (`CJGuirao/AMP-Templates:main`), if it isn't there already.
4. Create a **cjguirao-clubn-ai-bot** instance on the Linux host with Docker support. Give the
   container a memory limit of about 3.5 GB and 2 to 4 CPU cores.
5. In the instance's **Configuration > ClubÑ AI Bot**, set at least **Discord Bot Token** and
   **Server (Guild) ID**, and preferably **Allowed Channel IDs** and **Moderation Log Channel ID**.
6. Press **Update**. It downloads this folder from GitHub, installs it into
   `/AMP/clubn-ai-bot/venv`, and downloads the model (about 1 GB).
7. The first Update copies the Club Ñ rules (`rules.md`, a moderation summary of the official
   rules with minor protection first) and the knowledge files (full official rules, support and
   contact, roles, category guide) into the instance folder. Edit them in AMP's File Manager; later
   updates never overwrite your edits.
8. **Start.** The instance turns "Ready" when the console prints `ClubN bot ready`.

To update the bot later, press Update and restart. "Bot Source Version" can pin a tag or commit.

## Settings (all in AMP)

| AMP setting | Variable | Default |
|---|---|---|
| Discord Bot Token | `CLUBN_DISCORD_TOKEN` | required |
| Server (Guild) ID | `CLUBN_GUILD_ID` | blank = global command |
| Slash Command Name | `CLUBN_COMMAND_NAME` | `pregunta` |
| Allowed Channel IDs / Role IDs | `CLUBN_ALLOWED_CHANNEL_IDS` / `CLUBN_ALLOWED_ROLE_IDS` | blank = all |
| Moderation Log Channel ID | `CLUBN_MOD_LOG_CHANNEL_ID` | blank |
| Answer When Mentioned | `CLUBN_ANSWER_ON_MENTION` | off |
| System Prompt | `CLUBN_SYSTEM_PROMPT` | blank (`\n` = new line) |
| Personality | `CLUBN_PERSONALITY` | friendly Club Ñ veteran, informal "tú" |
| Answer Model | `CLUBN_MODEL` | `hf.co/LiquidAI/LFM2.5-24B-A2B-GGUF:Q4_K_M` |
| Rules Check Model | `CLUBN_CHECK_MODEL` | `hf.co/LiquidAI/LFM3-1.7B-GGUF:Q4_0` |
| Unload Model After Idle | `CLUBN_KEEP_ALIVE` | `30m` |
| Context Size | `CLUBN_NUM_CTX` | `8192` |
| CPU Threads | `CLUBN_NUM_THREADS` | `0` (auto) |
| Reasoning Mode | `CLUBN_THINK` | off |
| Model Request Timeout | `CLUBN_LLM_TIMEOUT` | `300` |
| Extra Server Rules | `CLUBN_RULES_TEXT` | blank (`\n` = new line) |
| Cooldown / Daily Limit / Max Length / Queue | `CLUBN_COOLDOWN_S` / `CLUBN_DAILY_LIMIT` / `CLUBN_MAX_QUESTION_CHARS` / `CLUBN_QUEUE_SIZE` | 60 / 30 / 600 / 5 |
| Check Answers Before Posting | `CLUBN_CHECK_OUTPUT` | on |
| Blocked Patterns | `CLUBN_BLOCKED_PATTERNS` | blank (regexes separated by `;;`) |
| Web Search Provider / SearXNG URL | `CLUBN_WEB_SEARCH` / `CLUBN_SEARXNG_URL` | `ddgs` |
| Wikipedia Languages | `CLUBN_WIKIPEDIA_LANGS` | `es,en` |
| Web Results / Pages Read In Full | `CLUBN_MAX_WEB_RESULTS` / `CLUBN_FETCH_PAGES` | 5 / 2 |
| Blocked Source Domains | `CLUBN_BLOCKED_DOMAINS` | blank |
| Bot Source Repository / Version | `CLUBN_SOURCE_REPO` / `CLUBN_SOURCE_REF` | `CJGuirao/AMP-Templates` / `main` |
| Timezone | `TZ` | `Europe/Madrid` |
| Log Level | `CLUBN_LOG_LEVEL` | `INFO` (Debug also logs unusable raw model replies) |

## System prompt and personality

"System Prompt" and "Personality" in the AMP settings shape the answers: what to focus on, format,
tone. They are placed before the bot's fixed rules, and the prompt tells the model the fixed rules win
on any conflict. More importantly, the guarantees don't depend on the model obeying: the rules check
happens before (with its own prompt, which these settings don't touch), and after the model answers
the code still drops any answer without a valid citation, strips links and mentions, and runs the
output check against the server rules. Both fields are limited to 2000 characters each.

## Security

- **The model is never exposed.** Ollama runs inside the instance container bound to `127.0.0.1`,
  the template publishes no ports, and the bot refuses any non-loopback model URL.
- **The token** exists only as an AMP setting. The start script refuses to run without it, Ollama is
  started without it in its environment, and the bot deletes it from its own environment after
  reading it. It is never written to disk or logged.
- **Least privilege on Discord:** no privileged intents by default, minimal permissions, and all
  `@everyone`, role and user pings are disabled on every message the bot sends.
- **Prompt injection:** user text and web text are neutralised so they can't close the prompt's
  delimiters or forge chat-template tokens; both are labelled as untrusted data; the moderation
  verdict must be one known label and the answer is constrained to a JSON schema; known injection phrases are refused before
  the model sees them; the answer is checked again before posting; links in the answer body are
  removed so only the cited source list carries URLs.
- **Web fetching** only reaches public IPs on ports 80 and 443, re-checks every redirect, and caps
  pages at 1.5 MB.
- **Abuse limits:** per-user cooldown and daily cap, one question on the CPU at a time with a short
  queue.
- AMP itself should stay behind a firewall or VPN, as for the other templates in this repository.

## Testing without Discord

From the instance console or a shell in the container (with the same variables set):

```
cd /AMP/clubn-ai-bot/app
../venv/bin/python -m clubn_bot check
../venv/bin/python -m clubn_bot ask "¿En qué año se fundó el Real Madrid?"
```

Unit tests: `python -m pytest -q tests` (they use a stand-in model and run in CI).
