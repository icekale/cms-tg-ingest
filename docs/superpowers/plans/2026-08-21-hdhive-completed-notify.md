# HDHive Completed Notify Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send one Telegram text the first time an HDHive TV subscription flips to `completed`.

**Architecture:** Pure formatter in `app/hdhive_cards.py`. `HdhiveSubscriptionService.check()` remembers the inbound status and calls optional `on_subscription_completed` only on a flip. `bridge.py` sends `telegram.send_message` to the subscription `chat_id`. Completion rules stay unchanged.

**Tech Stack:** Python 3, unittest, existing HDHive subscription service and Telegram client.

**Files:**
- Modify: `app/hdhive_cards.py`, `app/hdhive_subscriptions.py`, `bridge.py`, `tests/test_hdhive_cards.py`, `tests/test_hdhive_subscriptions.py`, `PRODUCT.md`, `README.md`, `docs/dockerhub-overview.md`, `CHANGELOG.md`

---

### Task 1: Formatter

- [x] Add `format_hdhive_subscription_completed` tests and implementation.
- [ ] Expected text:

```text
#12 剧名 已完结。
TMDB Ended，预期 10 集均已入队、已在 Emby 或已过滤。
之后不再每日检查。可在订阅里点恢复。
```

### Task 2: Service flip callback

- [x] `on_subscription_completed(subscription, summary)` on first flip only.
- [x] Callback errors are logged; check still returns `completed`.
- [x] Already-completed re-check does not notify.

### Task 3: Bridge + docs

- [x] Factory and `notify_hdhive_subscription_completed` in `bridge.py`.
- [x] PRODUCT / README / dockerhub / CHANGELOG mention one Telegram notice.

---
