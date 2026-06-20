---
name: Bug report
description: Report a cms-tg-ingest problem
title: "[Bug]: "
labels: [bug]
---

## Problem

Describe what happened and what you expected.

## Environment

- cms-tg-ingest version or image tag:
- CMS version:
- Deployment platform: Unraid / Docker Compose / other
- Workflow mode: direct / self_share_sync

## Redacted diagnostics

Run this inside the container and paste the output after checking it contains no secrets:

```sh
/app/scripts/diagnostics.sh
```

## Logs

Paste only redacted logs. Remove Telegram tokens, CMS passwords, Emby API keys, 115 cookies, and OpenAI keys.
