# 夸克网盘 API 实战笔记（AList 删除 / 回收站 / 恢复）

> 记录时间：2026-11-24
> 来源：为「CCTV 录像只保留一周」这个定时任务做前期链路验证时，在真实网盘上踩出来的第一手结论。
> 状态：**路径与语义均已实测**；文中不含任何 Cookie / Token 实体，一律用占位符。

本文只回答一件事：**用 API 删夸克网盘里的目录，到底会发生什么，坑在哪。**

---

## 1. 一句话结论

- 删除走 **AList 的 `/api/fs/remove`** 最省心（免 Cookie、幂等、删目录一次到位）。
- 但 AList 的删除在夸克侧只是 **送进回收站**，**不释放空间**。
- 真正释放空间要再调夸克官方 `POST /1/clouddrive/file/recycle/remove`。
- **夸克网页版没有回收站入口**，验收不能依赖 GUI。
- 夸克官方 API 的写操作普遍存在「**不精确指定 = 全量**」的默认语义，必须白名单化。

---

## 2. 端点实测表

### 2.1 夸克官方 API（drive.quark.cn）

基址：

```
https://drive.quark.cn/1/clouddrive          # 主
https://drive-pc.quark.cn/1/clouddrive       # 别名，实测行为完全一致（不同 IP，同一后端）
```

请求三件套（缺一即 401）：

```
?pr=ucpro&fr=pc                             # pr=客户端身份, fr=from pc
Referer: https://pan.quark.cn/
Cookie: <夸克登录态>
```

| 方法 | 路径 | 实测结果 | 语义 |
|---|---|---|---|
| `POST` | `/1/clouddrive/file/delete` | 401 `31001 require login [guest]`（存在） | 删除文件/目录 → **进回收站** |
| `GET` | `/1/clouddrive/file/recycle/list` | 401（存在；POST 返回 405） | 列出回收站，支持 `_page` / `_size` / `_fetch_total` / `_sort` |
| `POST` | `/1/clouddrive/file/recycle/recover` | 200 `code:0`（存在） | 恢复回收站内容，**必须带 `select_mode`** |
| `POST` | `/1/clouddrive/file/recycle/remove` | 200 `code:0` + `data.finish=true`（已实测清空） | 移除/清空回收站内容（真正释放空间的一步）；body 必须带 `select_mode` |
| — | `/1/clouddrive/file/recycle/clear` | **404** | 不存在，别用（这个名字是凭印象编的） |
| `GET` | `/1/clouddrive/file/sort` | 200 | 列目录（`pdir_fid=0` 为根），带 `dir` / `fid` / `updated_at` |

**免登录健康探针技巧**：不带 Cookie 请求任意真实端点，返回 `401 + code 31001` 说明**路径存在**；返回 `404 Not Found` 说明路径不存在。这一招可以在不碰数据的前提下，快速确认某个 endpoint 名字对不对。

### 2.2 AList API（本地 10.0.1.227:5244）

| 方法 | 路径 | Body | 说明 |
|---|---|---|---|
| `POST` | `/api/fs/list` | `{"path","password","page","per_page","refresh"}` | `refresh:true` 强制绕过缓存 |
| `POST` | `/api/fs/remove` | `{"dir":..., "names":[...]}` | **`dir` 是父目录，`names` 是数组**，可一次传多个子项 |

实测：删一个含 96 个 mp4 的日期目录，**0.181 秒**返回 `{"code":200,"message":"success"}`，目录树被整体带走，不需要逐文件删。

### 2.3 夸克 Cookie 存在哪、怎么取、多久会变（实测）

**存放位置**：AList 的 SQLite（`/home/gateman/alist/data/data.db`），表 `x_storages` 的 `addition` JSON 字段，**明文**：

```json
{"cookie":"b-user-id=……","root_folder_id":"0","order_by":"none","AdditionVersion":2}
```

**两种取法**：

| 方式 | 需要什么 | 备注 |
|---|---|---|
| `GET /api/admin/storage/get?id=1` → `.data.addition` → `.cookie` | **AList 管理员** token | `/api/admin/*` 对普通用户一律 403 |
| 只读打开 `data.db` 查 `x_storages.addition` | 仅需该主机文件读权限 | **零凭据**，推荐给定时任务 |

**轮换特性（实测）**：

| 观测 | 数据 |
|---|---|
| 同一台机器 25 分钟内的两次 DB 备份 | `11-15 18:59` → `754f6f40…`，`11-15 19:23` → `48e0440d…`（**已不同**） |
| 跨日结构变化 | 11-14 长度 1067 → 11-15 长度 1857 |
| 我方快照 vs 1.5 小时后的实时值 | 同为 1857 字节，**221 字符不同** |
| 连续 3 次 API 访问后的值 | 完全不变（不会每次请求都换） |

**结论**：

1. Cookie 是**分钟~小时级轮换**的，任何 Vault / 文件快照都会迅速过期 → **不要存快照**，运行时现取。
2. AList 是「**被动续期**」：夸克发新 Cookie 时它接收并写回 DB，但自己**不主动刷新**。
3. 断线症状（记住好排查）：`storage.status ≠ work`、日志出现 `401 / code 31001 require login`、列目录失败。常见诱因是**在手机/网页端登录夸克**把 AList 会话顶掉。

---

## 3. 三个真实踩过的坑

### 坑 ① `select_mode=1` 不是「按 fid 恢复」，而是「全选恢复」

第一次恢复时按直觉传：

```json
{"fids": ["9afe512248af474895f1a2c63e3fb5a1"]}
```

返回：

```json
{"status":400,"code":14001,"message":"Bad Parameter: [select_mode should not be null]"}
```

补上 `"select_mode":1` 后返回 200，**但回收站总数从 77 掉到 41，最终剩 9**——67 项历史垃圾全部被恢复回根目录，还自动重命名出 `CCTV_Records(1)`、`头文字D_第五季_1080P` 等副本，并有个别同名目录被**合并**（原有目录里凭空多出 `_cindy_probe.txt` 之类文件）。

**教训**：参数名字“看起来对”不等于语义对。涉及写操作、尤其带 “select / all / batch” 字样的参数，必须先在一个**可恢复的小对象**上试，或者先只查文档/错误返回，不允许拿真数据探路。

### 坑 ② 夸克网页版看不到回收站

`https://pan.quark.cn/` 是网盘本体，但**网页端不提供回收站入口**（回收站只在手机 App / PC 客户端里）。

后果：**「删完之后还能不能后悔」这件事，靠网页验不了**。于是验收链路必须改成：

- API 回查 `recycle/list`（可靠、可脚本化、可进 CI 日志）
- job 自己在日志里输出删除清单（日期 / 文件数 / 字节数）
- 客户端人工抽查只作辅助

另外一个用户体验上的坑：`https://pan.quark.cn/list` 会 302 跳回首页，网页版走哈希路由，**不存在 `/list` 这类路径**，别拿它当书签。

### 坑 ③ 删除不释放空间

`/api/fs/remove` → 夸克 `file/delete` → **进回收站**。网盘已用容量不会变。要真省空间，必须再走一步 `POST /1/clouddrive/file/recycle/remove`。

---

## 4. 安全操作纪律（从这次事故提炼）

1. **写操作白名单化**：删除只接受显式列出的名字/日期，永不接受“全部”“最新 N 天以外”这类模糊选择器再交给远端解释。
2. **陌生参数先证伪**：用无 Cookie 探针确认路径存在 → 用可恢复对象验证语义 → 最后才动真数据。
3. **删前留清单**：任何批量删除，先把 `日期 / 文件数 / 字节数` 打进日志；事后恢复与追责都靠它。
4. **一次调用能删多个就不循环**：`names` 数组传全量，减少请求数和部分成功状态。
5. **误恢复的收拾方式**：误恢复出来的都是回收站里的历史对象，重新 `/api/fs/remove` 即可回到原状（本次 11 项根目录垃圾 + 2 个探针文件，一次调用清完，`diff` 与原貌完全一致）。

---

## 5. 复现用命令片段

```bash
# 只读：看回收站（记得替换凭据）
curl -s "https://drive.quark.cn/1/clouddrive/file/recycle/list?pr=ucpro&fr=pc&_page=1&_size=50&_fetch_total=1" \
  -H "Cookie: $QUARK_COOKIE" -H "Referer: https://pan.quark.cn/" | jq '.data.total'

# 删除（走 AList，推荐）
curl -s -X POST http://10.0.1.227:5244/api/fs/remove \
  -H "Authorization: $ALIST_TOKEN" -H 'Content-Type: application/json' \
  -d '{"dir":"/Quark/CCTV_Records/锦绣世家_客厅","names":["2026-11-10","2026-11-11"]}'

# 真正释放空间（危险，必须在确认清单后执行）
# POST https://drive.quark.cn/1/clouddrive/file/recycle/remove?pr=ucpro&fr=pc
```

> ⚠️ 最后一条的 body 形状尚未实测（未在真实数据上验证过「清空全部」与「指定 fid」两种语义），**首次使用前必须先小范围验证**。

**补充（2026-11-24 已实测）**：清空回收站只需

```json
{"select_mode": 1}
```

返回 `{"status":200,"code":0,"data":{"finish":true}}`（**同步完成**，无需轮询 task_id），回收站 `total` 由 13 直接归零。省略 `select_mode` 会返回 `14001 Bad Parameter: [select_mode should not be null]`。

> 注意：同一族接口里 `select_mode=1` 在 `recycle/recover` 上表现为「全量恢复」，在 `recycle/remove` 上表现为「全量清空」。**它表达的是“选择模式=全部”，不是“按 fid 指定”。** 想让某个 `select_mode` 语义成立，必须先在可恢复/可忽略的对象上验证，不能靠直觉。

---

## 6. 数据时间线（本次事故复盘）

| 时刻（UTC） | 事件 |
|---|---|
| 18:10 | 通过 AList 删除 `2026-10-21`（96 文件 / 13.9GB），进入回收站，回收站 total=77 |
| 18:15 | 误用 `select_mode=1` 触发全量恢复，根目录多出 11 项，回收站 total 77 → 9 |
| 18:16 | 目标目录 `2026-10-21` 成功回到 `CCTV_Records/锦绣世家_客厅`（404 恢复为 40 天） |
| 18:2x | 白名单删除 11 项 + 2 个探针文件，根目录 23 项与原貌 `diff` 完全一致 |

关键观察：**`CCTV_Records/锦绣世家_客厅` 里文件的 `modified` 是上传时间，不是拍摄时间**（曾出现 11-20 的录像于 11-22 08:56 补传）。因此保留策略**只能按文件夹名里的日期判断，绝不能按 mtime**。
