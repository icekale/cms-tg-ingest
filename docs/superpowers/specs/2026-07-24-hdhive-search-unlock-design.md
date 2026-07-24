# HDHive Search And Unlock Design

Date: 2026-07-24
Status: approved for planning

## Goal

Add HDHive search and unlock operations to the existing single-user Telegram bot without requiring a separate HDHive OpenAPI application key.

The integration will:

- match a movie or series through CMS TMDB search;
- query HDHive resources through the authorization proxy already used by CMS;
- filter resources by cloud drive type;
- unlock one resource or a selected batch using the HDHive account bound in CMS;
- hand unlocked 115 links to the existing TaskStore ingest workflow;
- show the account quota, point balance, expected cost, and partial failures in Telegram.

## Scope

This version uses the one HDHive account already authorized in CMS. The existing `TG_ALLOWED_CHAT_ID` restriction remains the authorization boundary.

Included:

- Telegram search and button interactions;
- movie and TV TMDB matching;
- HDHive resource listing and cloud drive filtering;
- single and batch unlock;
- quota and point information;
- automatic intake of unlocked 115 links;
- HDHive status in the existing health check;
- unit and integration tests with fake HTTP responses.

Not included:

- separate OAuth accounts for multiple Telegram users;
- direct use of an HDHive application secret;
- HDHive subscription polling;
- automatic ingest of non-115 cloud drive links;
- changes inside the encrypted CMS image.

## Integration Choice

The add-on will share the CMS OAuth access token read-only and call the same authorization proxy used by CMS.

The alternatives were rejected:

- Running CMS internal Python modules through `docker exec` requires Docker socket access and depends on private CMS implementation details.
- Patching the CMS image would be lost after an update.
- Waiting for CMS to expose search and unlock endpoints would block the feature.

The token file is mounted read-only from the CMS configuration directory. The add-on accesses only the parsed `access_token` field. It does not access, use, copy, persist, refresh, or log the `refresh_token` field.

When the authorization proxy reports an expired access token, the add-on calls the authenticated CMS `/api/hdhive/info` endpoint. CMS owns the refresh operation and updates its token file. The add-on then reloads the access token and retries the original request once.

## Components

### `HdhiveProxyClient`

Responsibilities:

- load the current access token from the mounted CMS token file;
- call the configured authorization proxy;
- query the bound account;
- query resources by `movie|tv` and TMDB ID;
- unlock a single slug or a list of slugs;
- normalize proxy responses into typed application data;
- redact tokens and unlocked URLs from diagnostic logs;
- ask `CmsClient` to refresh the account on token expiry and retry once.

Required operations:

```text
account() -> HdhiveAccount
resources(media_type, tmdb_id) -> list[HdhiveResource]
unlock(slugs) -> HdhiveUnlockResult
healthcheck() -> bool
```

### CMS client additions

`CmsClient` will add a read-only `get_hdhive_info()` method for account status and token refresh delegation. Existing CMS login and automatic re-login behavior remain unchanged.

TMDB matching will use the CMS endpoints already used by its web application:

```text
GET /api/tmdb/search_movie
GET /api/tmdb/search_tv
```

No separate TMDB key is required for this Telegram flow.

### Telegram search sessions

One in-memory search session is kept per allowed chat. A session contains:

- a short random session ID;
- the original query;
- TMDB candidates;
- selected media type and TMDB ID;
- normalized HDHive resources;
- active cloud drive filter;
- selected resource indexes;
- creation and expiry times.

Sessions expire after 15 minutes. Restarting the process clears them; the user can repeat the search. No OAuth token or unlocked URL is stored in a session.

Telegram callback data uses the short session ID and numeric indexes so it stays below Telegram's 64-byte callback limit. Mutations are guarded by a lock because callback updates and polling can overlap.

## Telegram Flow

1. The persistent menu contains `HDHive 搜索`.
2. The bot asks for a title or TMDB ID.
3. The next text message is searched against CMS movie and TV TMDB endpoints.
4. The bot shows candidates with title, year, media type, and TMDB ID.
5. Selecting a candidate queries HDHive through the authorization proxy.
6. The bot defaults to the `115` filter and offers buttons for every available `pan_type` plus `全部`.
7. Resource rows show title, size, resolution, source, validation state, unlock points, and whether the account already owns the resource.
8. Each selectable row has a single-unlock button and a selection toggle. The footer contains batch unlock and cancel buttons.
9. Unlock results are summarized per resource.
10. Every successful 115 `full_url` is submitted through the same intake helper used by an incoming Telegram 115 link.
11. Non-115 links are returned to the user but are not submitted to TaskStore.

Invalid resources are visible as unavailable so the user can understand why the result count changed, but they cannot be selected. Unknown validation status remains selectable and is labeled as unverified.

## Cost And Confirmation Rules

Before unlock, the bot queries the bound account and displays:

- nickname;
- point balance;
- weekly free quota and remaining count;
- whether the free quota is unlimited;
- maximum possible point cost for the selected resources.

For a single resource:

- `unlock_points <= 20` executes immediately after the unlock button is pressed;
- `unlock_points > 20` or an unknown cost requires a second confirmation button;
- an already-owned resource executes immediately because HDHive should not charge again.

For a batch:

- all resources at or below 20 points can be unlocked from the batch button;
- if any resource is above 20 points or has unknown cost, the whole batch requires confirmation;
- the message states that HDHive decides whether to consume free quota or points;
- the maximum point cost is the sum of known costs for resources not already owned.

The client enforces HDHive batch limits before the request:

- normal user: 1;
- VIP: 5;
- forever VIP: 10.

The bot does not split an oversized batch automatically because doing so can change quota and point consumption between calls.

## Error Handling

Expected errors are mapped to user-facing messages:

- missing or unreadable token file: authorize HDHive in CMS first;
- expired access token: ask CMS to refresh and retry once;
- expired refresh authorization: reauthorize from the CMS HDHive account page;
- insufficient points: show current balance and required maximum;
- resource not found or invalid: disable or report that resource only;
- batch partial failure: report successful links and each failed slug separately;
- authorization proxy timeout or rate limit: retain the search session and allow retry;
- 115 intake failure after unlock: keep the unlocked link in the Telegram result so it can be retried manually.

No exception should stop Telegram polling. Search and unlock network calls use the existing HTTP timeout and bounded retry behavior.

## Configuration

New environment variables:

```text
HDHIVE_ENABLED=false
HDHIVE_PROXY_BASE_URL=https://authx.771885.xyz
HDHIVE_TOKEN_CONFIG_PATH=/config/hdhive-openapi.json
HDHIVE_SEARCH_SESSION_TTL_SECONDS=900
HDHIVE_AUTO_UNLOCK_MAX_POINTS=20
```

Deployment adds this read-only mount:

```text
/mnt/user/appdata/cloud-media-sync/config/hdhive-openapi.json:/config/hdhive-openapi.json:ro
```

The feature remains disabled when the variable is false or the token file is unavailable. Existing ingest behavior is unchanged.

## Verification

Automated coverage will verify:

- token parsing without exposing refresh tokens;
- resource normalization and `pan_type` filtering;
- movie and TV TMDB matching;
- callback session validation and expiry;
- single unlock at and above the 20-point boundary;
- batch limit enforcement and high-cost confirmation;
- partial batch failures;
- token-expiry refresh delegation to CMS;
- successful 115 results entering TaskStore once;
- non-115 results not entering TaskStore;
- health check behavior and secret redaction.

Live verification will first perform account and resource queries only. A real unlock is performed only after the Telegram confirmation UI shows the selected resource, account quota, and expected maximum cost.
