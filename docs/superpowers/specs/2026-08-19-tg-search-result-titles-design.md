# Telegram HDHive Search Result Titles Design

Date: 2026-08-19
Status: approved for planning

## Goal

Search result movie and series names are present but unreadable. Candidate buttons currently look like `1. 片名 (年份) [电影]`, truncate the title to 34 characters with a head-and-tail cut, and hide the name on a phone keyboard. After a candidate is chosen, the resource page header is only `movie / TMDB 550`.

This change makes the full title the primary thing the user reads, without changing the search source or the unlock flow.

## Scope

In scope:

- Candidate result message: numbered list with the full title, year, media type, and TMDB ID.
- Candidate buttons: `序号. 片名` only. Long names truncate at the end with `…`. Button text stays at or under Telegram's 64-character limit.
- Resource page header: include the selected title, year, and Chinese media type, not just `media_type / TMDB id`.
- Unit tests for button labels, truncation, the candidate message, and the resource header.

Out of scope:

- Telegram `/` command menu and `setMyCommands`.
- Persistent reply keyboard / `/help` menu.
- Resource-row buttons (filename, pan type, resolution, cost).
- Replacing CMS TMDB search with a direct TMDB client.
- A per-title wizard, posters, or pagination.
- Session schema changes.

## User-visible copy

Candidate message:

```text
请选择要查询的 TMDB 媒体：
1. 攻壳机动队 SAC_2045 (2020) · 剧集 · TMDB 80986
```

- Title in the message is never truncated.
- Year stays `年份未知` when missing.
- Media type is `电影` or `剧集`.
- Keep at most 12 candidates, same as today.

Candidate buttons:

- Select: `1. 攻壳机动队 SAC_2045`
- TV only, unchanged: `订阅此剧`
- Footer, unchanged: `取消搜索`

Resource header after a candidate is selected:

```text
HDHive 资源：攻壳机动队 SAC_2045 (2020) · 剧集 · TMDB 80986
```

If the selected candidate title is missing, use `未命名`. Resource rows and filter/unlock buttons stay as they are.

## Design

1. Add a trailing-ellipsis helper (do not reuse `truncate_text`, which cuts head and tail). Candidate button titles use this helper so the readable prefix is kept.

2. Change `hdhive_candidate_keyboard` in `app/telegram_ui.py`:
   - Button text is `f"{index + 1}. {title}"`.
   - `title` is `candidate["title"]` or `未命名`.
   - Truncate the whole button string to 64 characters with a trailing `…`.
   - Year and media type stay off the button.
   - Subscribe and cancel callbacks stay the same.

3. Change the candidate `send_message` text in `bridge.py` to the copy above. Title, year, and TMDB ID come from the existing `search_candidates` dict (`title`, `year`, `media_type`, `tmdb_id`). Still read `title` / `name` only; do not add new CMS fields.

4. Change `format_hdhive_resources` in `bridge.py` to resolve the selected candidate from `session.candidates` by matching `media_type` and `tmdb_id`. Use that title and year in the header. No new session fields.

## Error handling

- Empty title → `未命名` on both the message line and the button.
- No matching candidate for the resource header → `未命名` plus the existing media type and TMDB ID.
- Telegram 64-character button limit is enforced in the keyboard builder. Callback data is unchanged (`hive:candidate:{session_id}:{index}`) and already fits in 64 bytes.
- Search failures, empty results, and session expiry keep the existing messages.

## Testing

- Candidate button text is `1. Example` / `2. Example TV`, with no year or `[电影]` / `[剧集]` on the select button.
- A title long enough to overflow 64 characters ends with `…` and the button text length is `<= 64`.
- The candidate message still contains the full untruncated title, year, Chinese media type, and TMDB ID.
- `format_hdhive_resources` header includes the selected candidate title.
- Existing subscribe-for-TV-only keyboard test still passes.

## Files

- `app/telegram_ui.py`
- `bridge.py`
- `tests/test_hdhive_bridge.py`
