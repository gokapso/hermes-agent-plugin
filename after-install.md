# Kapso WhatsApp Plugin Installed

Enable the plugin if you did not pass `--enable`:

```bash
hermes plugins enable kapso
```

Set the optional outbound defaults in `~/.hermes/.env`:

```bash
KAPSO_PHONE_NUMBER_ID=...
KAPSO_HOME_CHANNEL=15551234567
```

Configure your Kapso webhook:

| Setting | Value |
| --- | --- |
| Endpoint URL | `https://<your-public-host>/kapso/webhook` |
| Events | `whatsapp.message.received` |
| Payload version | `v2` |
| Secret | Same value as `KAPSO_WEBHOOK_SECRET` |

Restart the gateway after enabling or changing env vars:

```bash
hermes gateway restart
```
