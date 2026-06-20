# cms-tg-ingest

`cms-tg-ingest` is a small Telegram companion service for Cloud Media Sync (CMS). Send a 115 share link to your Telegram bot and the service submits it to CMS, tracks the workflow, optionally creates your own permanent 115 share, generates share-link STRM through CMS share-sync, moves the STRM folder into the matching Emby library path, confirms Emby import, and optionally deletes the transferred 115 source file to free space.

The service does not provide media resources and does not bypass 115, CMS, or Emby permissions. It automates a workflow you already have access to.

## Features

- Telegram bot ingestion for one or more bare 115 links in a message.
- Single allowed Telegram user/chat.
- SQLite history, duplicate detection, `/status`, `/history`, `/metrics`, `/health`, `/quality`, and `/clear_history`.
- CMS-first recognition; asks for category only when recognition is uncertain.
- Optional OpenAI-compatible fallback classification.
- Self-share workflow: transfer, wait for CMS organize, create permanent 115 share, call CMS share-sync, move generated STRM.
- Emby confirmation with media library name.
- Optional cleanup of the transferred 115 source after Emby confirmation while keeping your own share alive.
- Offline `doctor.py` checks for required config and mounted paths.

## Quick start

```sh
git clone https://github.com/your-name/cms-tg-ingest.git
cd cms-tg-ingest
cp .env.example .env
# edit .env
docker compose up -d --build
```

Run diagnostics:

```sh
docker compose exec cms-tg-ingest python /app/doctor.py
```

Run tests locally:

```sh
python3 -m py_compile bridge.py doctor.py
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

## Required configuration

Edit `.env` before starting:

- `TG_BOT_TOKEN`: Telegram bot token from BotFather.
- `TG_ALLOWED_CHAT_ID`: only this chat/user can operate the bot.
- `CMS_BASE_URL`: CMS web URL, for example `http://192.168.1.10:9527`.
- `CMS_USERNAME` and `CMS_PASSWORD`: CMS login.
- `DB_PATH`: normally `/data/submissions.db`.

Secrets must stay in `.env`. Do not commit `.env`.

## Recommended self-share workflow

Set:

```env
WORKFLOW_MODE=self_share_sync
P115_COOKIE_PATH=/config/115-cookies.txt
SELF_SHARE_STRM_ROOT=/mnt/user/Unraid/strm/share
SELF_SHARE_CMS_LOCAL_PATH=/media/share
SELF_SHARE_CLEANUP_AFTER_EMBY=true
MOVE_CONFLICT_POLICY=merge
```

Expected flow:

1. You send a 115 share link to the Telegram bot.
2. CMS transfers the share to your configured pending folder and auto-organizes it.
3. The companion finds the organized 115 folder.
4. The companion creates your own permanent 115 share with receive code `1212`.
5. The companion calls CMS share-sync using your own share link.
6. CMS writes STRM under `SELF_SHARE_STRM_ROOT`.
7. The companion moves/merges that STRM folder into the mapped library folder.
8. The companion confirms Emby sees the item and reports the media library name.
9. If enabled, the companion deletes only the transferred 115 source file/folder and keeps your own share active.

## Path mapping

The container paths must match your `.env` paths. For Unraid, a common compose mount is:

```yaml
volumes:
  - ./data:/data
  - /mnt/user/Unraid/strm:/mnt/user/Unraid/strm:rw
  - /mnt/user/appdata/cloud-media-sync/config/115-cookies.txt:/config/115-cookies.txt:ro
```

`CMS_PARENT_CID_CATEGORY_MAP` maps your CMS organized 115 folder CIDs to categories. This is environment-specific; leave it blank to disable parent-CID inference, or set it to your own CMS folder IDs.

```env
CMS_PARENT_CID_CATEGORY_MAP=3260485903797190075=欧美电影,3254119954860998447=外国电视
```

`STRM_LIBRARY_MAP` uses comma-separated `分类=路径` entries:

```env
STRM_LIBRARY_MAP=欧美电影=/mnt/user/Unraid/strm/转存/Movie/电影/欧美电影,外国电视=/mnt/user/Unraid/strm/转存/TV
```

Only paths under configured source and library roots are moved.

## Telegram commands

- Send a bare `https://115cdn.com/s/...?...` link: submit and process.
- `/help`: show supported commands.
- `/status`: recent tasks.
- `/history`: longer task history.
- `/metrics`: counters.
- `/health`: service-level checks.
- `/quality`: records that need attention.
- `/clear_history`: clear completed local history.


## Published images

Release tags publish multi-arch Docker images to GHCR automatically. This repository also publishes Docker Hub images under `icekale/cms-tg-ingest`:

```sh
docker pull ghcr.io/icekale/cms-tg-ingest:0.1.0
docker pull icekale/cms-tg-ingest:0.1.0
```

Docker Hub publishing is optional. To enable it, add these GitHub repository secrets:

- `DOCKERHUB_USERNAME`: Docker Hub namespace or username.
- `DOCKERHUB_TOKEN`: Docker Hub access token.

Create a release image by pushing a semantic version tag:

```sh
git tag v0.1.0
git push origin v0.1.0
```

The release workflow publishes `linux/amd64` and `linux/arm64` images. Tag pushes also publish `latest`; manual workflow runs publish SHA-tagged images.

## Diagnostics

Inside the container:

```sh
python /app/doctor.py
/app/scripts/diagnostics.sh
```

The diagnostics script redacts environment variables whose names contain `TOKEN`, `PASSWORD`, `KEY`, `COOKIE`, or `SECRET`. Still review output before posting it publicly.

## Safety notes

- Keep `.env`, 115 cookies, Telegram tokens, Emby API keys, and OpenAI keys private.
- Pin image versions in production. Avoid blind `latest` upgrades.
- Test with a small known link before bulk use.
- `SELF_SHARE_CLEANUP_AFTER_EMBY=true` deletes the transferred 115 source only after the companion confirms Emby import and STRM movement. It does not cancel your own permanent share.
- This project depends on CMS, 115, Telegram, and Emby APIs. Those services can change behavior without notice.

## Development

```sh
python3 -m py_compile bridge.py doctor.py
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

No third-party Python package is required for the current runtime.

## License

MIT.
