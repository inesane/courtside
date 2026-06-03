#!/usr/bin/env python3
"""Send a real test push notification using subscriptions from the local DB."""

import os, sys, json
sys.path.insert(0, ".")
from database import get_all_push_subscriptions

subs = get_all_push_subscriptions()
if not subs:
    print("No push subscriptions in local DB.")
    print("Open http://localhost:5050, click Enable on Push Notifications, then re-run this.")
    sys.exit(1)

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
if not VAPID_PRIVATE_KEY:
    print("Set VAPID_PRIVATE_KEY env var first.")
    sys.exit(1)

from pywebpush import webpush, WebPushException

payload = json.dumps({
    "headline": "Test push from Courtside",
    "detail": "If you see this, push notifications are working!",
    "priority": "high",
})

print(f"Sending to {len(subs)} subscription(s)...")
for sub in subs:
    print(f"  → {sub['endpoint'][:60]}...")
    try:
        webpush(
            subscription_info=sub,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": "mailto:admin@courtside.app"},
        )
        print("  ✓ Sent")
    except WebPushException as e:
        print(f"  ✗ Failed: {e}")
