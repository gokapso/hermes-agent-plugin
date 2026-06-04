# Kapso Hermes Platform Plugin

Kapso WhatsApp platform adapter for Hermes Agent. It receives Kapso platform
webhooks, turns inbound WhatsApp messages into Hermes `MessageEvent`s, and sends
Hermes replies through Kapso's WhatsApp Cloud API proxy.

## Install

Copy this directory into a Hermes platform plugin path:

```bash
hermes plugins install gokapso/hermes-agent-plugin --enable
```

For private-repo installs, use the SSH URL instead:

```bash
hermes plugins install git@github.com:gokapso/hermes-agent-plugin.git --enable
```

If HTTPS asks for a GitHub username/password, the repo is private or your server
cannot read it anonymously. GitHub no longer accepts account passwords for Git.
Either make the repo public, or add an SSH key to GitHub and use the SSH command
above.

Hermes clones Git plugins into `~/.hermes/plugins/kapso` and loads them on the
next session or gateway restart.

Install the only runtime dependency if your Hermes environment does not already
include it:

```bash
pip install aiohttp
```

## Configure

The easiest path is the plugin setup command:

```bash
hermes kapso setup --install-cli
```

That command saves values to `~/.hermes/.env`, optionally installs the Kapso CLI
with `npm install -g @kapso/cli`, and prints the webhook settings to paste into
Kapso.

If you prefer to set env vars manually:

```bash
KAPSO_API_KEY=...
KAPSO_WEBHOOK_SECRET=...
KAPSO_PHONE_NUMBER_ID=...      # recommended for outbound and cron delivery
KAPSO_HOME_CHANNEL=15551234567 # optional default recipient for deliver=kapso
KAPSO_ALLOWED_USERS=15551234567 # recommended for production allowlisting
```

Useful follow-up checks:

```bash
hermes kapso status
kapso status
kapso whatsapp numbers list --output json
```

To allow a specific WhatsApp user after setup:

```bash
hermes kapso setup --allowed-users 15551234567 --no-prompt
hermes gateway restart
```

For development-only open access:

```bash
hermes kapso setup --allow-all-users --no-prompt
hermes gateway restart
```

The adapter listens on `0.0.0.0:8648` and accepts `POST /kapso/webhook` by
default. Configure the Kapso webhook to point at your public URL for that path.

Recommended Kapso webhook settings:

| Setting | Value |
| --- | --- |
| Events | `whatsapp.message.received` |
| Payload version | `v2` |
| Secret | Same value as `KAPSO_WEBHOOK_SECRET` |

Signature verification is on by default. For unsigned local fixtures only:

```bash
KAPSO_VERIFY_WEBHOOK_SIGNATURES=false
```

## Chat IDs

Inbound sessions use encoded IDs:

```text
kapso:<base64url(phone_number_id)>:<base64url(wa_id)>[:<base64url(conversation_id)>]
```

For manual sends or cron delivery, you can use either a plain WhatsApp recipient
when `KAPSO_PHONE_NUMBER_ID` is configured:

```text
15551234567
```

or an explicit phone number ID and recipient:

```text
<phone_number_id>:15551234567
```

## Config YAML

Environment variables are the easiest path, but this also works:

```yaml
gateway:
  platforms:
    kapso:
      enabled: true
      extra:
        api_key: "..."
        webhook_secret: "..."
        phone_number_id: "..."
```

## Notes

- `hermes plugins install ... --enable` prompts for `KAPSO_API_KEY` and
  `KAPSO_WEBHOOK_SECRET` automatically when they are missing.
- `hermes kapso setup --install-cli` is safe to rerun when you need to rotate
  keys or add `KAPSO_PHONE_NUMBER_ID`.
- Hermes can run the Kapso CLI after installation, so you can ask the agent to
  inspect numbers or webhook state. Enter secrets through the installer/setup
  prompts rather than pasting them into chat.
- Text messages are split at WhatsApp's 4096-character limit.
- Outbound Markdown links are converted to `label (url)`, and `**bold**` is
  converted to WhatsApp's `*bold*` style.
- Media messages currently land as captions or descriptive placeholders. The
  next useful extension is downloading `kapso.mediaUrl` into Hermes media
  caches when Kapso includes mirrored media.
