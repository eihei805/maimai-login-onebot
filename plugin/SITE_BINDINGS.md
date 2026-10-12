# Private site bindings

`site_bindings.py` handles only private OneBot messages for one QQ at a time:

- `/绑定水鱼 TOKEN` stores the Diving-Fish import token.
- `/绑定落雪 PERSONAL_TOKEN` stores an LXNS personal token. The uploader may use the personal API without asking for a friend code; this binding layer never invents one.
- `/解绑水鱼` and `/解绑落雪` remove one binding.
- `/上传状态` reports only configured/unconfigured state and never prints a token.
- `/token`, `/获取token`, or a bare `/绑定水鱼` / `/绑定落雪` shows both token acquisition links: https://maimai.diving-fish.com/ (logged-in personal profile, 成绩导入 Token) and https://maimai.lxns.net/user/profile (personal API key). Status/help includes these links; no automated login or key generation is performed.

Tokens are accepted only as visible ASCII without whitespace, CR/LF, or NUL, up to 2048 characters. The exact token is passed to the upload adapter; this layer does not add or remove `Bearer`.

Credentials live under `private`, which must be an owner-only `0700` directory. Each QQ has `qq-<decimal QQ>.json` with owner-only `0600` mode. A `0600` lock serializes readers and writers; files are written to a `0600` temporary file, fsynced, atomically replaced, and the directory is fsynced. Symlinks, hard-linked files, malformed stores, unsafe QQ values, and wrong modes are rejected. No credential or exception text is logged or sent to QQ.

Integration is explicit. The parent can inject:

```python
site_bindings.configure_upload(
    uploader=lambda score_upload_module, result, bindings, catalogs: ..., 
    catalog_provider=lambda result: (fish_catalog, lxns_catalog),
)
```

The callback imports `score_upload` lazily and passes the documented inputs to the injected uploader. The adapter must return per-site dictionaries with a fixed `state`; only a small allowlist of states is shown to the user. If no adapter/catalogs are configured, upload is reported as unavailable. `upload_callback(result, send)` is suitable for `QRSync(on_result=...)`.

Use `SiteBindings(root=temporary_directory)` in tests. Tests should use fake senders/providers/uploaders and never network or production credential paths.

`main.configure_private_sync()` connects the adapter to `score_upload.upload_scores` and the provider to local public `data/music_data.json` and `data/songs.json` caches. Private binding commands are dispatched before QR handling. No global developer token is used by uploads, and missing/malformed caches fail closed without a network refresh.

`QRSync.before_capture = site_bindings.snapshot` freezes the requesting QQ and its credentials before capture starts. The immutable, redacted `UploadContext` is attached to the in-memory `CaptureResult.upload_context`; it is not written into the capture JSON. The callback requires a matching owner snapshot and uses its original credentials, including an originally empty binding. Rebinding or unbinding during a running job affects future jobs only; the running job continues against the original target. Missing/mismatched snapshots block uploads. The global QR gate is held through capture and read/merge/write, serializing this single process's upload jobs even when two QQs share a target. Multiple plugin processes or external writers still require coordination.

The `bindings` adapter argument contains only `fish_import_token` and/or `lxns_personal_token`, owned by the job requester. Do not log snapshots, catalog response bodies, request headers, or upstream error text. Tests use socket-free fake capture, temporary stores/public caches and a fake uploader transport. No actual uploads were performed during development.
