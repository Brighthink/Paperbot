# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Three research-group bots, run with a local LLM and no external LLM API. [README.md](README.md) is the full reference: setup, config tables, Slack/Zulip app setup, per-feature behaviour, and a code map of every module (section 8).

| Package | Role | Runs |
|---|---|---|
| `paperbot` | Semantic Scholar/arXiv → relevance filter → PDF summary → Slack/Zulip | Daily, from `scripts/loop.sh` |
| `articlebot` | Company research blogs (RSS) → scored backlog → one summarized post per organisation per day | Right after paperbot, same runner |
| `zoterobot` | Saves papers to Zotero + Google Drive; tool-calling DM chat agent over the library | Always on (Slack Socket Mode / Zulip) |

## Environment

- Production runs from `~/paperbot` on a shared Linux GPU server, where `llama-swap` on `127.0.0.1:8090` serves one model (`qwen3.8:27b`) on GPUs 1 and 2. Anything that needs the LLM, including a `--dry-run`, needs that server.
- Real configs are gitignored and must be copied from the examples: `config.yaml` (repo root, paperbot), `articlebot/config.yaml`, `zoterobot/config.yaml`.
- Secrets come from `~/.paperbot_env`, which the `scripts/*.sh` runners source. When running a bot by hand, source it first.
- All three packages share one venv. Install CPU `torch`/`torchvision` before `requirements.txt`; a CUDA torchvision next to a CPU torch fails with `operator torchvision::nms does not exist`.

```bash
python3 -m venv venv && source venv/bin/activate
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
```

## Commands

There is no build step and no lint or formatter config. `tests/` is gitignored and local-only, so this repository has no test suite to run and tests must not be committed. Check changes with the commands below.

```bash
# paperbot
python -m paperbot --dry-run --topic <name> --limit 1      # one topic, one paper, print instead of post
python -m paperbot --skip-filter --dry-run --limit 2       # bypass the relevance filter
python -m paperbot --backfill --dry-run                    # ignore the arXiv lookback window
python -m paperbot --backfill-chat [--limit N] [--topic NAME]   # fill the chat cache (no GPU, no posting)

# articlebot
python -m articlebot --list                                # backlog status (no LLM, no posting)
python -m articlebot --dry-run [--source NAME] [--limit 1] # print + save to preview_dir
python -m articlebot --dry-run --url <post URL>            # one post, outside feeds and backlog
python -m articlebot --ingest-only --rejudge               # re-score after changing judge criteria

# zoterobot
python -m zoterobot --check                                # verify Zotero, Drive, Slack scopes, Zulip
python -m zoterobot --ask "질문"                            # chat agent in the terminal, with tool-call trace
python -m zoterobot --save GDL 2505.13631                  # save one paper without a chat platform
python -m zoterobot --sync [--force]                       # build or refresh the full-text cache
```

**`--dry-run` differs between bots.** paperbot's only replaces the publisher, so papers are still recorded as `done` in the real DB. To test without touching it, copy the DB and pass a temporary config with `--config`. articlebot's runs against an in-memory copy of its DB.

## Architecture

### Packages depend on each other

The packages are separate but not independent. Changing these modules can break another bot:

- `paperbot.llmserver`: loaded-model check, server context, safe unload. Used by articlebot and zoterobot.
- `paperbot.publisher` and `paperbot.publisher.zulip`: Zulip client, message layout, math spacing, Slack markdown conversion. articlebot and zoterobot import from here, including private names such as `_NamedBytes`.
- `paperbot.extractor`, `paperbot.models`, `paperbot.summarizer`, `paperbot.config`: zoterobot reuses PDF download and text extraction, `Paper`, and the summarizer. `summarize_paper` loads paperbot's own config through `chat.paperbot_config`.
- `zoterobot.textcache`: paperbot and articlebot write into it through their `chatdb.py`. paperbot imports it lazily.

### The chat cache is the integration point

`library_text.db` (`zoterobot.textcache`) is one sqlite store written by separate processes. `storage.chat_text_db` in the paperbot and articlebot configs must point at the same file as zoterobot's `chat.text_db`; empty disables the hook.

- Zotero/Drive items come from `zoterobot/sync.py`.
- Published feed documents use `item_type` `paperbot_paper` or `blog_article`, with ids of `P`/`A` plus 7 hex chars of the uid hash (`feed_key`), so re-runs overwrite. The Zotero sync excludes them.
- zoterobot notices outside writes through `refresh_if_changed()` before every DM.
- A cache write failure after publishing is only a warning.

### State and pipelines

- paperbot `papers` table: `done`, `filtered_out`, `pending` (full JSON in `payload`), `expired`, `failed` (retried once), `failed_permanent`. Pending papers go first, FIFO, then new ones up to the per-topic, per-source quota.
- articlebot `articles` table has the same shape, plus `seen`. uid is the canonical post URL, and `pending` is the backlog. Ingest runs every `fetch.interval_days`, tracked in the DB, while reporting runs daily.
- One paper failing does not stop a run; only a publish configuration error aborts it. Filters and settings belong in config, not code.
- Config files load into frozen dataclasses with validation in each package's `config.py`. A new key needs the dataclass, the loader, the `*.example.yaml`, and the README table.

### LLM server rules

- Every LLM call sends `chat_template_kwargs: {"enable_thinking": false}`. Keep this on new calls, or the model spends thousands of tokens before answering.
- `llm.num_ctx` (paperbot) and `chat.ctx_tokens` (zoterobot) must equal llama-server's `-c`. paperbot's code default is 32768 while the example config and server use 65536.
- GPU and VRAM limits exist in two places: `VRAM_GPUS`/`VRAM_MIN_MIB` in `scripts/loop.sh`, and `chat.vram_gpus`/`chat.vram_min_mib` in zoterobot's config. They also have to match `CUDA_VISIBLE_DEVICES` in the llama-swap config.
- Never unload the model directly. llama-swap's unload cuts off a streaming request, so go through `paperbot.llmserver`, which waits until the server reports no running or queued requests. articlebot also skips the unload while paperbot's lockfile is held.

### zoterobot chat agent

Limits are enforced in code, not in the prompt: `budget.py` gives each question a token pool, tool output is sized to it, and each round recalibrates the chars-per-token estimate from the server's measured input tokens. Tool definitions and the system prompt are in `prompts.py`, and the streaming tool loop is in `agent.py`. `MultiBot` in `listener.py` routes Slack and Zulip into the same handlers, with platform differences in `slackbot.py`, `zulipbot.py`, and `messages.py`.

Zulip output uses LaTeX (`$$...$$` inline, ```` ```math ```` blocks), while Slack gets plain text. The summary prompts branch on platform.

## Conventions and gotchas

- Code comments, docstrings, logs, and user-facing messages are mostly in Korean, and many module docstrings open with an English line. Example configs are commented in both languages. Code identifiers are English.
- **Replay is temporary.** `paperbot/replay.py` and every line tagged `[REPLAY]` will be removed once old papers are in Zulip. Keep new logic out of it and tag any hook you must add; `grep -rn "\[REPLAY\]"` finds them all, and the two `_select` lines lose only the `replay_items` argument.
- **`scripts/loop.sh` is read lazily by a running bash.** Replace the file rather than editing it in place, and recreate the tmux session for the change to apply. Python code and config changes need no restart, because each daily run is a fresh process.
- **Restarting zoterobot**: SIGTERM drains in-flight questions for up to `drain_timeout_s` before exiting. Do not use `kill -9` on the listener.
- `ai.meta.com/blog` is deliberately excluded from articlebot because its robots.txt forbids automated collection.
