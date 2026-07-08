#!/usr/bin/env python3
"""Build webhook_subscriptions.json from manifest.yaml + prompt files + secrets.

Usage:
    python3 build-webhooks.py                    # build to default location
    python3 build-webhooks.py --output /tmp/test.json
    python3 build-webhooks.py --secrets /path/to/secrets.json
    python3 build-webhooks.py --dry-run          # show what would change
"""

import argparse
import hashlib
import json
import os
import sys

import yaml


def load_manifest(manifest_path):
    with open(manifest_path) as f:
        return yaml.safe_load(f)


def load_secrets(secrets_path):
    """Load secrets from JSON file. Returns empty dict if file doesn't exist."""
    if os.path.isfile(secrets_path):
        with open(secrets_path) as f:
            return json.load(f)
    return {}


def build_subscriptions(manifest_dir, manifest, secrets):
    """Assemble the final webhook_subscriptions dict."""
    result = {}
    subs = manifest.get("subscriptions", {})

    for name, cfg in subs.items():
        # Read prompt file
        prompt_file = cfg.get("prompt_file")
        if not prompt_file:
            print(f"ERROR: {name} has no prompt_file", file=sys.stderr)
            sys.exit(1)

        prompt_path = os.path.join(manifest_dir, prompt_file)
        if not os.path.isfile(prompt_path):
            print(f"ERROR: prompt file not found: {prompt_path}", file=sys.stderr)
            sys.exit(1)

        with open(prompt_path) as f:
            prompt_text = f.read()

        # Build the subscription entry (all fields from manifest)
        entry = {}
        for k, v in cfg.items():
            if k == "prompt_file":
                continue  # replaced by actual prompt content
            entry[k] = v

        # Inject secret (from secrets file or environment variable)
        secret_val = secrets.get(name)
        if not secret_val:
            env_key = f"WEBHOOK_SECRET_{name.upper().replace('-', '_')}"
            secret_val = os.environ.get(env_key)
        if secret_val:
            entry["secret"] = secret_val

        # Reorder keys to match canonical format: description, events, prompt/secret, rest
        ordered = {}
        # description first
        if "description" in entry:
            ordered["description"] = entry.pop("description")
        # events second
        if "events" in entry:
            ordered["events"] = entry.pop("events")
        # prompt and secret next (mimics original layout)
        ordered["prompt"] = prompt_text
        if "secret" in entry:
            ordered["secret"] = entry.pop("secret")
        # remaining fields in original order
        for k in entry:
            ordered[k] = entry[k]

        result[name] = ordered

    return result


def content_hash(data):
    return hashlib.sha256(json.dumps(data, indent=4, ensure_ascii=False).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Build webhook_subscriptions.json")
    parser.add_argument(
        "--manifest",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webhooks", "manifest.yaml"),
        help="Path to manifest.yaml",
    )
    parser.add_argument(
        "--output",
        default=os.path.expanduser("~/.hermes/webhook_subscriptions.json"),
        help="Output path for webhook_subscriptions.json",
    )
    parser.add_argument(
        "--secrets",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webhooks", "secrets.json"),
        help="Path to secrets.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be built without writing",
    )
    args = parser.parse_args()

    manifest_dir = os.path.dirname(os.path.abspath(args.manifest))
    manifest = load_manifest(args.manifest)
    secrets = load_secrets(args.secrets)

    subscriptions = build_subscriptions(manifest_dir, manifest, secrets)

    # Check existing output
    existing_hash = None
    if os.path.isfile(args.output):
        with open(args.output) as f:
            try:
                existing = json.load(f)
                existing_hash = content_hash(existing)
            except json.JSONDecodeError:
                existing_hash = None

    new_hash = content_hash(subscriptions)

    # Print summary
    sub_names = list(subscriptions.keys())
    print(f"Subscriptions: {len(sub_names)}")
    for name in sub_names:
        sub = subscriptions[name]
        prompt_len = len(sub.get("prompt", ""))
        has_secret = "secret" in sub
        skills = sub.get("skills", [])
        events = sub.get("events", [])
        print(f"  {name}:")
        print(f"    events={events} skills={skills} prompt={prompt_len}chars secret={'yes' if has_secret else 'NO'}")

    if existing_hash == new_hash:
        print(f"\nNo changes detected (hash: {new_hash[:12]}...). Output unchanged.")
        return

    if existing_hash:
        print(f"\nChanges detected!")
        print(f"  Old hash: {existing_hash[:12]}...")
        print(f"  New hash: {new_hash[:12]}...")

        # Show what changed
        with open(args.output) as f:
            existing = json.load(f)

        for name in set(list(existing.keys()) + list(subscriptions.keys())):
            in_old = name in existing
            in_new = name in subscriptions
            if in_old and not in_new:
                print(f"  - REMOVED: {name}")
            elif in_new and not in_old:
                print(f"  + ADDED: {name}")
            elif content_hash(existing[name]) != content_hash(subscriptions[name]):
                print(f"  ~ MODIFIED: {name}")
    else:
        print(f"\nNew output (no existing file at {args.output})")

    if args.dry_run:
        print("\n[DRY RUN] Would write to:", args.output)
        return

    # Write output
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(subscriptions, f, indent=4, ensure_ascii=False)
        f.write("\n")

    print(f"\nWrote: {args.output}")


if __name__ == "__main__":
    main()
