# Self-Share Name And Password Design

## Goal

Keep CMS-organized 115 folder names unchanged by default and use one authoritative receive-code resolver for every newly created self-share.

## Naming

- New tasks keep the CMS canonical folder name, such as `H-黑金-2011-[tmdb=77221]`.
- The `share_alias_prepared` stage remains in the task graph but becomes a no-op for new tasks.
- Existing tasks with `share_alias_name` and a canonical manifest remain supported by STRM discovery, restoration, move, and repair code.
- This change does not rename existing 115 folders automatically.

## Receive Code Resolution

The preferred receive code for a newly created self-share is resolved in this order:

1. Web UI runtime setting stored in TaskStore.
2. CMS `cms_config` row `share_115_sync`, field `SHARE_115_PASSWORD`.
3. `SELF_SHARE_OWN_SHARE_PASSWORD` environment variable.
4. `1212`.

The resolved code is passed to the 115 permanent-share update request. The actual code returned by the client is persisted as `own_share_receive_code`; all CMS share sync, STRM validation, health, and repair paths continue using that task-specific persisted value.

## Web API

- Overview returns the configured receive-code source and a masked value only.
- A POST settings endpoint accepts a non-empty alphanumeric receive code without returning it in plaintext.
- Clearing the Web override restores CMS/environment/default resolution.

## Safety

- Existing tasks and STRM files retain their persisted receive code.
- Source-link passwords and own-share passwords remain redacted from read APIs and logs.
- A share in 115 review state remains pending; source cleanup remains blocked until the existing review checkpoints pass.
