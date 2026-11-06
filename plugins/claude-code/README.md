# AskHooman channel for Claude Code

Wakes a running Claude Code session when you act in the AskHooman app: you answered, closed or reopened one of
its asks, or replied on one of its tasks. The session gets a short message such as

    AskHooman: your user answered ask #12 — call asks_mine

and reads what happened with the AskHooman MCP tools. The message carries only the event type and ids, never
your text. No server or public URL is needed: the plugin keeps one long poll open to askhooman.com
(`GET /api/agent/events`) with an agent key.

## Install

1. In the AskHooman app (Settings on iPhone and Mac, Agents on the web): Create agent key, with the name of the agent that already asks you
   (then "Use the existing agent"). Save the key:

       mkdir -p ~/.config/askhooman && pbpaste > ~/.config/askhooman/key && chmod 600 ~/.config/askhooman/key

2. In Claude Code:

       /plugin marketplace add alloverart/askhooman
       /plugin install askhooman@askhooman

3. Start Claude Code with the channel enabled (needed until the plugin is listed by Anthropic):

       claude --dangerously-load-development-channels plugin:askhooman@askhooman

Limits from Claude Code: channels work in interactive sessions with a claude.ai or Console sign-in. Elsewhere
use MCP `ask_wait`, or `asks.py listen --exec …` from this repository.

Environment: `ASKHOOMAN_KEY` (instead of the key file), `ASKHOOMAN_URL` (default `https://askhooman.com`).
Requires `python3` 3.9 or newer; nothing else to install.
