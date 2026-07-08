#!/usr/bin/env python3
"""Dispatch items to the local webhook receiver."""
import urllib.request
import json
import sys

DISPATCH_URL = "http://100.102.201.26:8646/api/dispatch"

def dispatch(item_id, prompt_id="default"):
    data = json.dumps({"item_id": item_id, "prompt_id": prompt_id}).encode()
    req = urllib.request.Request(
        DISPATCH_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        body = resp.read().decode()
        return {"status": resp.status, "body": body}
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return {"status": e.code, "body": body, "error": str(e)}
    except Exception as e:
        return {"status": 0, "error": str(e)}

if __name__ == "__main__":
    items = sys.argv[1:]
    for item_id in items:
        result = dispatch(item_id)
        print(f"{item_id}: {json.dumps(result)}")
