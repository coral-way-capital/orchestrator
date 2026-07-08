# Webhook Subscriptions

Git-tracked source of truth for all webhook subscriptions consumed by the Hermes gateway.

## How It Works

```
manifest.yaml          ← config for each subscription (events, skills, deliver, etc.)
webhooks/prompts/*.md   ← prompt text for each subscription
secrets.json            ← NOT in git — webhook secrets
         ↓
build-webhooks.py       ← assembles everything into webhook_subscriptions.json
         ↓
~/.hermes/webhook_subscriptions.json  ← gateway auto-reloads on change
```

The **manifest** defines each subscription's metadata (events, skills, deliver mode, skip_actions, etc.) **except** the `prompt` and `secret` fields. Those are injected at build time:

- **prompt** → read from the `.md` file referenced by `prompt_file`
- **secret** → read from `secrets.json` (or env var `WEBHOOK_SECRET_<NAME>`)

## Editing a Prompt

1. Edit the `.md` file in `prompts/`
2. Run: `python3 scripts/build-webhooks.py`
3. The gateway auto-reloads the updated JSON

## Adding a New Subscription

1. Add an entry to `manifest.yaml` under `subscriptions`
2. Create a new `prompts/<name>.md` with the prompt text
3. Add the secret to `secrets.json` (not committed)
4. Run: `python3 scripts/build-webhooks.py`

## Secrets

**Secrets are NOT in git.** Copy `secrets.example.json` to `secrets.json` and fill in real values:

```bash
cp webhooks/secrets.example.json webhooks/secrets.json
```

Alternatively, set environment variables:

```
WEBHOOK_SECRET_CWC_PR_REVIEW=... \
WEBHOOK_SECRET_CWC_PR_REVIEW_CYCLE=... \
  python3 scripts/build-webhooks.py
```

## Build Script

```bash
# Default: build to ~/.hermes/webhook_subscriptions.json
python3 scripts/build-webhooks.py

# Custom output
python3 scripts/build-webhooks.py --output /tmp/test.json

# Custom secrets location
python3 scripts/build-webhooks.py --secrets /path/to/secrets.json

# Preview without writing
python3 scripts/build-webhooks.py --dry-run
```

The build script only overwrites the output file if the content actually changed (preserves mtime if identical).
