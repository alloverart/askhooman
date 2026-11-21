# Installing the AskHooman MCP server (for AI agents)

AskHooman is a hosted MCP server: there is nothing to download, build or run. The agent connects to one URL:

```
https://askhooman.com/mcp
```

Transport: Streamable HTTP. Authorization: MCP OAuth 2.1 with PKCE. The user signs in with Google in the browser when the client first connects; no API key is needed.

## Cline

Add this to `cline_mcp_settings.json` (MCP Servers → Configure → Configure MCP Servers), or use the Remote Servers tab with the same URL and the Streamable HTTP transport:

```json
{
  "mcpServers": {
    "askhooman": {
      "type": "streamableHttp",
      "url": "https://askhooman.com/mcp",
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

Then authorize the server (Cline CLI: `cline mcp` → Authorize OAuth) and let the user finish the Google sign-in in the browser.

## Clients without OAuth

The user creates an agent key in the AskHooman app (https://app.askhooman.com, Connected agents → Create key; the key is shown once) and the client sends it as a header:

```json
{
  "mcpServers": {
    "askhooman": {
      "type": "streamableHttp",
      "url": "https://askhooman.com/mcp",
      "headers": { "Authorization": "Bearer <agent key from the app>" }
    }
  }
}
```

Never write the key into a file that is committed or shared.

## Check that it works

Call `asks_mine` and `tasks_mine`. Both return an empty list for a new account. Then create a test question with `ask_create` (title: "Test: can you see this?"). It appears in the user's AskHooman app on their phone or Mac.

## How to use it

Read the server's instructions (sent on connect) and the house rules at https://askhooman.com/agents. In short: one decision per ask, act on the answer, then `ask_close` with a one-line resolution.
