# Live QR score upload adapter

`score_upload.py` owns conversion and site HTTP access only. It reads no files,
environment variables, production tokens, QR values, or game endpoints. No external
writes were performed during implementation. Tests use synthetic catalogs and mocks.

## Integration contract (QR worker)

```python
from score_upload import Credentials, upload_scores

# Trusted worker: requester QQ -> private binding belonging to THAT requester.
credentials = Credentials(
    fish_import_token=binding.get('fish_import_token'),
    lxns_personal_token=binding.get('lxns_personal_token'),
    lxns_friend_code=binding.get('lxns_friend_code'),
)
results = upload_scores(
    capture.scores,                       # flattened native UserMusicDetail list
    fish_catalog,                        # data/music_data.json: list
    lxns_catalog,                        # data/songs.json: list or {'songs': list}
    credentials,
    complete=True,                        # ONLY after fetch completion validation
)
```

`prepare_scores(rows, fish_catalog, lxns_catalog) -> Prepared` is pure. Prepared
contains `fish`, `lxns` and `omitted_badges`; avoid logging/repr-ing its raw fields.
`upload_scores(rows, fish_catalog, lxns_catalog, credentials, *, complete=False,
transport=None) -> {'fish': result, 'lxns': result}` is the worker interface.
Test transport is `(method, url, headers, body_dict_or_list_or_None) ->
(status, response_headers, response_bytes)`. No CLI sender is provided.

The shared worker must pass **all complete rows**, not B50, e.g. complete 190-row
capture -> each enabled site gets a single 190-score POST. A synthetic 190-row
integration test covers this. Do not infer completion from row count: the worker
must validate the pagination/completion marker, all page responses and its logout
policy. `complete=False` blocks all HTTP. Worker must hold the per-account lock
through read/merge/write (two QQ bindings may reference the same site account).
Serialize across same site account where known. External concurrent uploads can
still race: these APIs provide no compare-and-swap/ETag transaction. No automatic
retry is safe after a POST timeout; reconcile first.

Binding is the QR task's responsibility. Obtain user tokens privately; never use
`lxns_api_token.txt`, a global developer token, nickname lookup, or another QQ's
binding as fallback. Token possession alone does not prove QR account ownership.
Bind the consenting QQ requester to the verified game identity AND target site
account. Store tokens with private permissions, redact echoes/logs/backups and
support removal. For LXNS personal keys this adapter GETs the token's authoritative player identity,
requires a positive integer friend code within the supported numeric range, and
uses the personal score route without any additional binding field. An optional
explicit `lxns_friend_code` is validated and checked against that identity before
writing. Developer keys still require an explicit verified friend code.
Fish Import-Token acts on its owner's account and has no friend-code routing:
private binding must independently establish that this is the user's intended
account. Do not invent a token or silently authorize a different account.

Developer LXNS support is explicit opt-in via `lxns_developer_token` plus verified
`lxns_friend_code` instead of personal token, never both. Requires third-party
read-score AND write-data permissions because existing badges are preserved.
Personal token uses **X-User-Token**, not Bearer (Bearer in frontend JavaScript is
session/OAuth auth and is not evidence for personal API-key authentication).

## Results and safety

Each site independently returns:

- `not_configured`: no bound token, zero requests.
- `blocked`: incomplete fetch or invalid conversion/catalog/credentials.
- `failed`: fixed `reason`; no upstream body, token, title or friend code echoed.
- `accepted_unverified`: API accepted a POST, `submitted` count,
  `requires_readback=True`, `omitted_badges` count.

Do not tell the user “both synced successfully” on partial acceptance or treat
`accepted_unverified` as persistence verification. Say “API accepted N submitted
scores; persistence not read back”, separately per site. Fish additionally requires
`updates + creates == submitted`; zero/missing/mismatched counts do not pass.
LXNS success envelope does not report per-row persistence. Failed POSTs can have
partially applied or completed remotely: `submitted: 0` in failure is not proof
that no upstream mutation occurred.

At most five calls for two sites: Fish GET existing + POST, LXNS personal GET
identity + GET scores + POST (developer uses two). No retries, no redirects,
strict HTTPS destination/method allowlist, TLS verification always on, no inherited
proxy/netrc/session credentials, socket timeout 20 seconds, bounded 8 MiB
response/request and 10,000 rows. Streaming download checks a 30-second elapsed
budget between chunks; a blocked read can additionally consume its read timeout.
429 stops that site without retry; caller must not schedule immediate retries.

Fish's `update_records` overwrites flags even when omitted: it defaults absent
FC/FS to empty strings. Therefore adapter must first read existing scores with the
same token and preserve the stronger site badge, achievement and DX score. Missing
or malformed reads fail before POST. LXNS similarly merges existing FC/FS. Unknown
native optional flags are omitted during conversion and existing site flags survive.
Unknown existing site flags block that site's write instead of clearing them.

## Catalog and field rules

Native row fields: integer `musicId`, `level` (0..4), `achievement` (0..1010000),
`deluxscoreMax` (nonnegative integer). `achievement / 10000.0` is always applied to
native values; `scoreRank` is unused since sites derive rank from achievement.
No play time, DX stars, or fake score rank is invented. Explicit playCount=0 rows are skipped; all-unplayed captures are blocked.

Native ID must resolve uniquely by **exact ID** in Fish public music catalog, then
exact title/type/difficulty maps to LXNS catalog. Fish IDs must not be invented by
adding 10000; LXNS IDs must not be invented by modulo. Unknown/ambiguous songs,
duplicate charts, absent chart levels and utage block conversion. STD/DX identity
comes from the catalog's `type`, not an ID-range guess. Validate DX score against
3 times chart notes where notes are available. Both cached public catalogs were
inspected structurally only; no score/credential file was opened.

## DLL enum evidence and limitations

Supplied Assembly-CSharp SHA256:
`e4f4631745ab976e29a2828d0919c1fca3f8a28888cea583a920d403d6412a91`.
Static evidence files in workspace (no game calls):

- `upload-map-enums-il.txt`: metadata constant values and `VOExtensions.Convert`
  RVA 0x98b4c/0x98bb8 copy `comboStatus` <-> UserScore.combo and `syncStatus` <->
  UserScore.sync directly; no index translation.
- `upload-map-labels-il.txt`: PlayComboflagIDEnum `.cctor` RVA 0x188684 and
  PlaySyncflagIDEnum `.cctor` RVA 0x188a30, including exact literal labels.
- `upload-map-setters-il.txt`: GameScoreList.CalcUpdate RVA 0x136804:
  IL 0x03fb..0x0412 sets 3 when theoretical combo equals perfect+critical;
  0x0419..0x0437 sets 2 when adding great covers all notes;
  0x043e..0x0463 sets 1 when additionally adding good covers all notes.
  Enum 4 is explicitly AllPerfectPlus. Thus combo 0/1/2/3/4 -> absent/fc/fcp/ap/app
  is supported by actual judgement branches, not Silver/Gold naming proximity.
- Sync enum constants: None=0, ChainLow=1, ChainHi=2, SyncLow=3, SyncHi=4,
  SyncPlay=5. SetSyncResult RVA 0x13390c IL 0x01bf..0x01d1 sets 5 when two players
  play the same song and no stronger sync result exists; 5 -> `sync` is closed.
  Other branches are preserved in evidence, but the website's fs/fsp/fsd/fsdp
  naming bridge is not independently closed here. **1..4 are omitted**, not
  guessed; unknown/missing future enums are omitted too, without blocking valid
  achievements. Omitted optional badges are counted in results. This means a new
  FS/FS+/FDX/FDX+ will not be imported yet; old site badges remain intact.

## Public endpoint evidence

Inspected 2026-10-12, no authenticated requests:

- https://github.com/Diving-Fish/maimaidx-prober/blob/280ec6936f983cfc309763553fa705d0f7ffccf3/database/routes/maimai.py
  `GET /player/records`, `POST /player/update_records`; full base is
  `https://www.diving-fish.com/api/maimaidxprober/player/` (the `/player/` segment
  must not be omitted). Snapshot `upload-map-fish-source.txt`.
- https://github.com/Diving-Fish/maimaidx-prober/blob/280ec6936f983cfc309763553fa705d0f7ffccf3/database/app.py
  `oauth_or_login_required` explicitly accepts `Import-Token`; snapshot
  `upload-map-fish-auth.txt`.
- https://maimai.lxns.net/docs/developer-guide official code blocks distinguish
  raw `Authorization: <developer key>` from `X-User-Token: <personal key>`.
- https://maimai.lxns.net/docs/api/maimai personal GET/POST
  `/api/v0/user/maimai/player/scores`, GET `/api/v0/user/maimai/player`; developer
  GET/POST `/api/v0/maimai/player/{friend_code}/scores`; score schema and enums.

Run `python3 -m unittest -v test_score_upload`. All tests synthetic/mock; live
compatibility and persisted data require a separately authorized real run and
readback. `main.py` now wires the private binding flow and QR completion callback;
see SITE_BINDINGS.md for the pre-capture owner/credential snapshot and cache-only
catalog provider. Run `python3 -m unittest test_private_integration test_qr_sync
test_site_bindings test_score_upload` for the socket-free integrated flow and race
regressions.
