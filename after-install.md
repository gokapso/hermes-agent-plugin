# Kapso WhatsApp Plugin Installed

Enable the plugin if you did not pass `--enable`:

```bash
hermes plugins enable kapso
```

Run the guided setup command:

```bash
hermes kapso setup --install-cli --funnel-url https://<your-funnel-host>
```

The plugin installer only asks for `KAPSO_API_KEY`. The setup command can then
install the Kapso CLI (`@kapso/cli`), list your connected WhatsApp numbers, ask
which one Hermes should use, generate `KAPSO_WEBHOOK_SECRET`, and create the
Kapso phone-number webhook for you.

When prompted, enter your own WhatsApp number/wa_id so Hermes can save
`KAPSO_HOME_CHANNEL` and `KAPSO_ALLOWED_USERS`.

If webhook creation fails, create it manually with these settings:

| Setting | Value |
| --- | --- |
| Endpoint URL | `https://<your-funnel-host>/kapso/webhook` |
| Events | `whatsapp.message.received` |
| Kind | `kapso` |
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
