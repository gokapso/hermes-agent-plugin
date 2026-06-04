# Kapso Hermes Platform Plugin

Kapso WhatsApp platform adapter for Hermes Agent. It receives Kapso platform
webhooks, turns inbound WhatsApp messages into Hermes `MessageEvent`s, and sends
Hermes replies through Kapso's WhatsApp Cloud API proxy.

## Install

Install and enable the plugin from GitHub:

```bash
hermes plugins install gokapso/hermes-agent-plugin --enable
```

Hermes clones Git plugins into `~/.hermes/plugins/kapso` and loads them on the
next session or gateway restart.

## Quickstart

Run the guided setup:

```bash
hermes kapso setup --install-cli
```

This saves values to `~/.hermes/.env`, optionally installs the Kapso CLI with
`npm install -g @kapso/cli`, and prints the webhook settings to paste into Kapso.

For a non-interactive setup:

```bash
hermes kapso setup \
  --api-key "$KAPSO_API_KEY" \
  --webhook-secret "$KAPSO_WEBHOOK_SECRET" \
  --phone-number-id "1041695002363992" \
  --home-channel "15551234567" \
  --allowed-users "15551234567" \
  --install-cli \
  --no-prompt
```

Restart the gateway after setup:

```bash
hermes gateway restart
hermes gateway status
```

Expose the adapter to Kapso. The adapter listens on `0.0.0.0:8648` and accepts
`POST /kapso/webhook` by default. With Tailscale Funnel, point the public URL at
that local port:

```bash
tailscale funnel reset
tailscale funnel --bg http://127.0.0.1:8648
tailscale funnel status
```

Set the Kapso webhook URL to:

```text
https://<your-funnel-host>/kapso/webhook
```

Recommended Kapso webhook settings:

| Setting | Value |
| --- | --- |
| Events | `whatsapp.message.received` |
| Payload version | `v2` |
| Secret | Same value as `KAPSO_WEBHOOK_SECRET` |

Verify the setup:

```bash
hermes kapso status
curl http://127.0.0.1:8648/health
curl https://<your-funnel-host>/health
```

## Configuration

The setup command writes these values to `~/.hermes/.env`:

```bash
KAPSO_API_KEY=...
KAPSO_WEBHOOK_SECRET=...
KAPSO_PHONE_NUMBER_ID=...      # recommended for outbound and cron delivery
KAPSO_HOME_CHANNEL=15551234567 # optional default recipient for deliver=kapso
KAPSO_ALLOWED_USERS=15551234567 # recommended for production allowlisting
```

To allow a specific WhatsApp user later:

```bash
hermes kapso setup --allowed-users 15551234567 --no-prompt
hermes gateway restart
```

For development-only open access:

```bash
hermes kapso setup --allow-all-users --no-prompt
hermes gateway restart
```

Useful Kapso CLI checks after `--install-cli`:

```bash
kapso status
kapso whatsapp numbers list --output json
```

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

## Quick Troubleshooting

If Hermes is not receiving messages:

```bash
hermes gateway status
hermes kapso status
curl http://127.0.0.1:8648/health
tailscale funnel status
journalctl --user -u hermes-gateway.service -f
```

Check that Tailscale Funnel proxies to `http://127.0.0.1:8648`, not an older
bridge process. The public webhook should end in `/kapso/webhook`.

Check that Kapso is sending `whatsapp.message.received` events with payload
version `v2`, and that the webhook secret in Kapso matches
`KAPSO_WEBHOOK_SECRET`.

If the gateway log says no user allowlist is configured, add your WhatsApp ID:

```bash
hermes kapso setup --allowed-users 15551234567 --no-prompt
hermes gateway restart
```

If outbound replies fail, confirm `KAPSO_API_KEY` and `KAPSO_PHONE_NUMBER_ID`
are set:

```bash
hermes kapso status
kapso whatsapp numbers list --output json
```

If images do not reach the agent, tail the gateway logs while sending a photo:

```bash
journalctl --user -u hermes-gateway.service -f | grep -i kapso
```

Successful image ingestion logs `cached inbound image ...` and writes the file
under `~/.hermes/cache/images`. If you only see `image message ... has no
downloadable media URL yet`, confirm the webhook payload includes either
`kapso.mediaUrl`/`kapso.media_url` or an image media `id`.

If voice notes do not transcribe, first confirm the plugin cached the audio:

```bash
grep -R "cached inbound audio\|User sent audio\|STT" ~/.hermes/logs/*.log
find ~/.hermes/cache/audio ~/.hermes/audio_cache -type f -mmin -10 -ls 2>/dev/null
```

Successful voice-note ingestion logs `cached inbound audio ...`. Hermes then
uses its configured STT provider to transcribe the cached file. For a no-key
local STT provider:

```bash
~/.hermes/hermes-agent/venv/bin/python -m pip install -U faster-whisper
hermes gateway restart
```

For OpenAI Whisper/transcribe instead, add `VOICE_TOOLS_OPENAI_KEY` to
`~/.hermes/.env` and restart the gateway. If old logs show voice notes cached
as `.opus`, update the plugin; Kapso voice notes are cached as `.ogg` for
OpenAI STT compatibility.

## Implementation Notes

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
- Inbound images are downloaded through Kapso, cached locally, and attached to
  Hermes `MessageEvent.media_urls` for native vision processing.
- Inbound audio and voice notes are downloaded through Kapso, cached locally,
  and attached to Hermes `MessageEvent.media_urls` for native STT processing.
- Other non-image media currently lands as captions or descriptive placeholders.
