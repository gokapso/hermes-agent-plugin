# Kapso WhatsApp Plugin Installed

Enable the plugin if you did not pass `--enable`:

```bash
hermes plugins enable kapso
```

Run the guided setup command:

```bash
hermes kapso setup --install-cli
```

It saves env vars to `~/.hermes/.env`, can install the Kapso CLI
(`@kapso/cli`), and prints the webhook settings below.

Allow at least one WhatsApp user before expecting inbound messages:

```bash
hermes kapso setup --allowed-users 15551234567 --no-prompt
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

Useful checks:

```bash
hermes kapso status
kapso status
kapso whatsapp numbers list --output json
```
