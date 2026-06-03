# Kapso Hermes Platform Plugin

Kapso WhatsApp platform adapter for Hermes Agent. It receives Kapso platform
webhooks, turns inbound WhatsApp messages into Hermes `MessageEvent`s, and sends
Hermes replies through Kapso's WhatsApp Cloud API proxy.

## Install

Copy this directory into a Hermes platform plugin path:

```bash
mkdir -p ~/.hermes/plugins/platforms
cp -R /Users/rgaona/Documents/kapso-hermes-plugin ~/.hermes/plugins/platforms/kapso
hermes plugins enable kapso-platform
```

Install the only runtime dependency if your Hermes environment does not already
include it:

```bash
pip install aiohttp
```

## Configure

Set these in `~/.hermes/.env`:

```bash
KAPSO_API_KEY=...
KAPSO_WEBHOOK_SECRET=...
KAPSO_PHONE_NUMBER_ID=...      # recommended for outbound and cron delivery
KAPSO_HOME_CHANNEL=15551234567 # optional default recipient for deliver=kapso
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

- Text messages are split at WhatsApp's 4096-character limit.
- Outbound Markdown links are converted to `label (url)`, and `**bold**` is
  converted to WhatsApp's `*bold*` style.
- Media messages currently land as captions or descriptive placeholders. The
  next useful extension is downloading `kapso.mediaUrl` into Hermes media
  caches when Kapso includes mirrored media.
