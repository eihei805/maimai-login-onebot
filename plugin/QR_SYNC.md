# 私聊 QR 取分

`main.handle` 在现有群路由之前分派私聊给 `qr_sync.DEFAULT_SYNC`。WS 和旧 SSE 共用此分派，回执使用已有鉴权 HTTP `send_private_msg`。群白名单不变；含 SGWCMAID 的群消息及敏感取分/绑定命令静默丢弃，不引用原消息。只接受纯文本消息段；不解析图片或 CQ 指令。

私聊 `/取分` 查看说明。首次必须明确发送 `/同意HTTP取分`，接受原始 HTTP QR 接口明文传输风险。然后发送 `/取分 SGWCMAID...` 或直接发送 QR 文本。`/撤销HTTP取分` 撤回未来授权，不能取消已经开始的任务。授权按事件 `user_id` 存储，不从消息正文或游戏 ID 推导 QQ。

`/login SGWCMAID...` 会登录并读取成绩后保留实际游戏会话；完成后使用 `/logout`，该命令由同一 QQ 的固定私有会话记录恢复客户端 cookie 与登录字段，只发送 `UserLogoutApi`，收到服务端 `returnCode=1` 后才删除会话记录。重复 `/login`、坏记录、权限不符、进程超时或登出失败均阻止新登录并保留记录，反馈为未确认。记录位于 `MAIMAI_PRIVATE_DIR/session-<QQ>.json`，目录 0700、文件 0600、原子更新；只写入已验证的 QQ 路径、登录会话字段和受限游戏 cookie，不保存 QR 或 token 输出。所有 `/login`、`/取分` 和直接二维码入口均保留会话。登录成功满 60 秒后允许 `/logout`，第 14 分钟自动尝试正常登出。

进程内全局非阻塞 semaphore 串行化取分，每 QQ 默认 60 秒冷却，不排队；后台线程让事件循环可继续给出忙碌回执。取分、手动退出与定时退出共用 flock 跨进程锁。HTTP 授权在 `MAIMAI_PRIVATE_DIR/qr-consents.json` 持久化，0600，原子更新；目录须为当前运行用户所有且 0700。不读取或输出生产配置。

子进程无 shell，QR 仅经 stdin；固定 `--live --qr-stdin --allow-http-qr --preload-profile --session-owner <QQ> --result <unique>`。沿用当前 Python，不新增依赖。客户端内部 180 秒限时，父进程 210 秒硬限时，stdout/stderr 丢弃。客户端负责以 O_EXCL/0600 创建唯一结果，读取时再次检查拥有者、权限及非符号链接。超时/失败只反馈固定消息，并明确登出未确认。完整/部分取分反馈成绩条数及实际登出状态，不发送原始成绩、凭据、QR 或游戏身份。

## 上传接口（供独立组件接入）

`CaptureResult(requester: str, path: pathlib.Path, scores: list[dict], summary: dict, upload_context: object | None = None)`：

- `requester` 为发送 QR 的 QQ；代表持有 QR 的请求者，不证明与游戏账号 ID 或站点账号相同。
- `scores` 保留 live 客户端字段 `musicId, level, playCount, achievement, deluxscoreMax, comboStatus, syncStatus, scoreRank, extNum1`，不猜测水鱼/落雪曲目 ID 映射。
- `summary` 含 `status`（complete/partial/failed）、`rows`、`logout`（confirmed/unconfirmed）；保留模式额外包含 `session`（active/unknown）。`active` 表示已确认登录并保存恢复信息，不证明游戏服务器此刻仍在线，服务器可能自行过期。
- `/login` 完整取分也会调用上传回调；上传完成并不表示退出游戏，回执会提示 `/logout`。部分结果不上传。
- `upload_context` 是取分前由绑定组件生成的进程内凭据快照；不写入结果 JSON。回调必须校验快照所有者，防止取分期间重新绑定静默改变上传目标。
- `path` 为本地唯一私有结果路径，文件内容沿用 live 输出 `{scores, summary}`；不在 QQ 暴露路径。原始结果不额外持久化 QQ 或 QR。结果文件保留供上传任务使用，部署者负责后续保留期限管理。
- `DEFAULT_SYNC.on_result = callback` 可安装 `callback(result, send_private)`；只对 complete 调用，捕获回调异常且不回显细节。此回调仍处于全局取分锁内，应自行限时并遵循上传授权。上传模块可 import qr_sync，qr_sync 不 import 上传模块，避免循环。

`main.configure_private_sync()` 已接入私聊绑定路由、取分前快照、上传回调和本地公共曲库缓存；完整取分才进入上传。绑定组件依据站点 API 证据建立每 QQ 的显式凭据绑定，不能拿请求 QQ 当游戏 ID 或好友码。

## 离线验证

在插件目录下运行：

```sh
python3 -m unittest test_qr_sync test_session_lifecycle
python3 -m unittest test_transport test_lookup test_b50 test_b50_sources test_jackets test_capture_prepare test_capture_upload
python3 -m py_compile main.py qr_sync.py
```

会话恢复包含 `Session`（游戏 userId、loginId、原始 loginDateTime、token、User-Agent identity）、原始 `LoginInputs` 和 host-only cookie；登出只使用原始登录时间，不重新扫码或登录。恢复时校验 QQ、配置目标和 chipId，禁止任意会话文件路径。正常退出仍依赖现有 HDD `UserLogoutApi` 协议在当前后端兼容，离线验证不能证明生产兼容性。登录响应丢失、或在提交成功与本地持久化之间崩溃，会留下 `unknown`，没有足够字段安全发送登出，此时 `/logout` 保持原样：报未确认且**不做任何归档**（游戏侧会话约 30 分钟后自行过期，登出行为不改）。需要立刻解除阻塞时，用私聊人工入口 `/清理会话`：该记录会被**改名归档**到 `MAIMAI_ORPHAN_ARCHIVE`（默认 `<PRIVATE>/archive`，需与 PRIVATE 同级 0700），因为 `unknown` 里没有任何登录字段，本来就没有会话可退。`/清理会话` 只对 `unknown` 生效；`active` 会先发一次**只读** `GetUserMusicApi` 探针：仅当游戏回以**结构完整的空响应**（`Empty decoded response` / `Empty HTTP body` / `Invalid JSON response`）——即它已不认识该会话——才把记录按 `expired-session-` 归档并解除阻塞；探针显示会话仍然有效则回复「请先 /logout」；网络/TLS/HTTP 层故障一律回复「暂时无法确认」且**不动任何记录**。这条路径同样不写游戏、不发网络登出，也不会把已失效会话说成已登出。记录关闭已确认但删除时崩溃时，下一次 `/logout` 只清理 closed 标记，不重放网络登出。会话操作使用跨进程互斥。

新测试严格使用合成 QR、临时目录、mock 子进程，禁用 socket。没有发送 QQ、运行真实 QR、发游戏网络请求或重启生产服务。已校正旧 B50 测试契约：每人 30 秒冷却、提示节流，以及 HTTP 429 的全局 Retry-After；未修改群限流实现。完整离线回归 128 项通过，包含插件入口的 /login → 双站模拟上传 → /logout 流程。

## 定时登出

登录成功后立即保存独立的 `login_success_epoch`，后续取分和保存 cookies 不重置计时。systemd 每约 10 秒扫描：满 840 秒尝试一次正常登出，发送前持久保存尝试标记，失败保留记录并通知 QQ，后续可人工 `/logout` 重试。服务重启后按原时间恢复；没有时间字段的旧记录不自动发送请求。接口接受不代表已实测游戏会话释放。

## 券库存查询

登录后发送 `/查券`，使用自己保留的 active 会话只读查询库存。私聊及配置的白名单群可用；群内回复查询摘要。查询不会重新登录、刷新会话计时或写入库存，仍沿用登录后 60 秒可手动登出、840 秒自动尝试登出的规则。忙时请稍后重试。

查询显示游戏侧观测的券 ID、库存和原样日期；未知券 ID 保留数字。网络失败显示无法判定，不会据此清理会话。库存、日期和查询成功均不代表付款、到账或可用权益。购券写入见下节。


## 购券与结算

`/购券 2`、`/购券 3` 使用当前 QQ 的保留会话，仅购买并保留库存（hold），不打歌、不消耗券。私聊与已配置白名单群可用。流程读取上架信息与库存，单次发送 `UpsertUserChargelogApi`，再读回核对库存；不确定结果不得自动重试。此接口记录不代表外部支付凭证。

部署者需在 `MAIMAI_PRIVATE_DIR` 下准备权限 0600 的 `ticket-machine-profile.json`（父目录 0700），结构为 `{"machine": {...}, "extend_flags": {...}}`。机台身份、版本及时间配置由部署者核对，不能把模拟配置当作真实机台认证。缺配置或有效会话时拒绝购买。`prepare_ticket_plan` 为当前用户生成独立 hold 计划，拒绝旧 buy_use 计划。

CLI `--game-charge` 只读查询上架券；`--ticket-purchase --session-owner QQ --settlement-plan PRIVATE/settlement-plan-....json` 执行经校验的计划，默认 hold。`--ticket-action buy_use` 是独立显式测试模式，会消耗券，QQ 购券入口不会调用。请求按会话永久标记一次尝试，网络超时后保留标记，避免重复写入。

结算依赖模块、完整字段 schema 和离线回归一并提供。`--settle-basic` 仅开放物品 kind=1/id=201 随 SD BASIC 全 Miss 结算的测试路径，需完整存档及明确计划。曲目校验读取 `plugin/data/music_data.json` 公共缓存（部署前准备）；hold 也沿用该缓存依赖。物品服务端保存仍需实际读回验证。

白名单群会核验原消息、发送者与消息内容；有权限时尝试撤回，无管理权限或撤回失败会提示公开风险后继续。二维码本地尝试后不再重复提交。登录后的 60/840 秒登出策略保持不变。
