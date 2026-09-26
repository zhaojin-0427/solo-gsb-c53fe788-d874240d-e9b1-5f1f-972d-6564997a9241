# 字体回退与字形审查台

本地 Web 应用，用于审计 **多语言文本在字体栈上的逐簇回退与实际字形整形结果**，
并内置 **字体资产许可与嵌入合规门禁**。上传 OpenType 字体，输入多语言文本并配置
字体栈、语言、书写方向与 OpenType 特性开关，系统会按 Unicode 字素簇
（grapheme cluster）执行回退，以 **HarfBuzz 实际整形结果**（而非仅查 cmap）
判定覆盖，逐簇给出实际字体、字形编号与名称、定位数据、连字合并情况与回退原因。
审查记录固定字体摘要与参数，可随时重新执行做一致性复核，并导出 JSON 报告。

同时，管理员可按使用场景（**Web / 桌面 / 印刷**）登记不可变的字体许可策略版本，
门禁会读取已上传字体的 `OS/2 fsType`、`name` 表许可描述（nameID 13）与
许可 URL（nameID 14）、集合字体（TTC）全部成员及 WOFF/WOFF2 解包摘要，
按字体栈组合检查嵌入限制、许可字段冲突与证据缺失，返回逐条证据和确定性的
放行 / 拒绝结论。

## 功能一览

- **按 Unicode 字素簇回退（UAX #29）**：组合附加符、变体选择符序列、ZWJ emoji
  序列等多码位簇不会被拆散；簇内任一码位缺字则整簇回退，保证基字与附加符来自
  同一字体。
- **以 HarfBuzz 整形结果判定覆盖**：出现 `gid 0 (.notdef)`、码位无任何输出字形
  覆盖，或未被连字字形吞并，才判定该簇缺字——比单纯查 cmap 更准确（特性替换、
  连字、阿拉伯连写等都纳入判定）。
- **正确处理复杂场景**
  - 组合附加符：基字形 + mark 字形及其定位偏移一并展示；
  - 变体选择符：检查 cmap format-14 变体序列，缺失时给出警告（HarfBuzz 会回落
    到基字形）；
  - 连字：`fi`、阿拉伯 Lam-Alef 等合并多个簇时，连字字形共享给每个被合并簇，
    并标记 `merges_grapheme_clusters`；
  - 双向文本：python-bidi 实现 Unicode 双向算法（UBA），按嵌入级别切分 run，
    RTL run 以 RTL 方向整形，输出标记视觉顺序；
  - Default_Ignorable 字符（ZWJ/ZWNJ/方向控制符等）无可见字形不计为缺字；
  - 支持 LTR / RTL / TTB（竖排，使用垂直度量）。
- **逐簇审计数据**：实际命中字体及栈位置、字形 gid / 名称、前进量、x/y 偏移、
  名义前进量、glyph extents（bearing/width/height）、码位与 Unicode 名称、
  脚本、bidi 级别、回退原因与警告；字形轮廓以 SVG 实时渲染。
- **回退尝试轨迹**：每次对某字体的整形尝试（覆盖 / 部分覆盖 / 字体栈耗尽）及
  未覆盖簇号完整记录。
- **可复核的审查记录**：保存时固定完整请求参数与字体摘要（SHA-256、字形数、
  UPEM、cmap 数、版本等）。「重新执行」会用固定参数重新整形并与存档结果逐簇
  比对，同时检测字体摘要漂移。相同输入 + 相同字体文件 → 一致结果。
- **JSON 报告导出**：已保存记录可下载自包含的 JSON 报告（含参数、字体摘要、
  完整结果）；即时分析也可在前端导出。
- **字体资产许可与嵌入合规门禁**
  - 按 **Web / 桌面 / 印刷** 三场景登记**不可变策略版本**（版本号线性分配、
    内容只追加不改写）：声明允许的 `fsType` 位掩码集合、禁用位、必需的
    `name` 许可字段（nameID 13 描述 / 14 URL，可强制 https）、许可文本
    禁止正则，以及冲突组合规则（`fstype-flag-pair`、`license-text-pair`、
    `fstype-flag-text`）。
  - 证据读取：`OS/2` 表 `fsType`（按真实 embedding types 解读，含保留位与
    低 4 位互斥类型枚举校验）、`name` 表 nameID 13/14、**TTC 全部集合成员**
    逐个枚举（成员间许可不一致即拒绝）、WOFF/WOFF2 解包前后大小与 sfnt
    SHA-256 摘要。
  - 按字体栈组合检查嵌入限制、栈内许可冲突、集合成员冲突与证据缺失，输出
    **逐条证据（code / 证据数据 / 涉及栈位与成员）与确定性 allow/deny**。
  - 支持带**理由**和**到期时间**的豁免（可撤销）：豁免绑定具体字体摘要、
    策略版本与检查代码；**已过期、已撤销、或字体摘要不匹配一律不放行**。
  - 策略发布采用 **CAS**（`expected_active_version`，冲突返回 409）；
    「发布冻结」原子固定 **字体摘要 + 策略版本 + 决定清单**，按请求幂等键与
    内容指纹双重幂等，重复请求返回同一决定（首次 201，重复 200）。
  - 提供页面复核（「许可合规门禁」标签页）、冻结决定列表与 **JSON 清单导出**。
  - **fsType 位定义修正（v1 → v2，只追加不改写）**：v1 基线曾把 fsType 的
    4 位嵌入*类型枚举值*错按独立*位位置*解读——`0x0002`（Restricted License
    embedding，受限禁止嵌入）被误标为「预览印刷」并被印刷默认策略放行，
    `0x0008`/`0x0100`/`0x0001` 也整体错位。已登记策略不可原地改写：启动时
    系统保留 v1 不可变登记事实（供历史冻结按原版本复核），**追加修正版 v2
    并通过 CAS 切换生效**；逐条证据、冻结清单与幂等发布此后统一引用 v2，
    历史冻结决定绝不重算。
  - **存量存证的派生标签重算**：升级前登记的字体，其存证中的派生字段
    （`fstype_flags` / `fstype_flag_labels` / `fstype_reserved_bits`）可能仍按
    旧位序记录（如 `0x0002` 被标为 `preview-print-only`）。读取存证用于
    **评估、发布与许可摘要**时，系统以数值 `fsType` 为准，在副本中按修正版
    位定义重算每个成员（含 TTC 全部成员）的派生字段，使逐条证据、新冻结
    清单与页面呈现一致；数据库中的原始存证、此前冻结的决定与其导出
    **一律保持原貌**，可在「已冻结决定」区对照复核新旧记录。
  - 首次启动自动为三个场景登记 v1 历史基线并追加激活修正版 **v2**
    （生效版本为 v2）。

## 技术栈

Python 3.11+ · FastAPI · Uvicorn · SQLite（标准库 sqlite3，gzip 压缩存档）
· fontTools · uharfbuzz (HarfBuzz) · regex（UAX #29）· python-bidi（UBA）
· 原生 JavaScript（无前端构建步骤）。

## 方式一：Docker Compose（推荐，一键启动）

```bash
docker compose up --build
```

启动后浏览器访问： **<http://localhost:8000>** ，在顶部标签页选择
「许可合规门禁」即可使用门禁；字形审查功能在「审查分析」页。

- 上传的字体与 SQLite 数据库保存在 Docker 卷 `glyph-data`（容器内 `/app/data`），
  容器重建后数据仍在（门禁策略版本、豁免与冻结决定同库存放）。
- 停止：`docker compose down`（加 `-v` 可同时删除数据卷）。

## 方式二：本地直接运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

访问 <http://127.0.0.1:8000>。

数据目录：`data/app.db`（数据库）、`data/fonts/<sha256>.otf`（解包后的 sfnt
字体文件，以 SHA-256 命名，天然去重）。

## 使用流程

1. **字体库** → 上传 `.otf/.ttf/.woff/.woff2/.ttc`（TTC 取第 0 个字体；
   WOFF/WOFF2 自动解包为 sfnt 后以其 SHA-256 建立身份）。
2. 点击字体卡片的「➕ 加入字体栈」，在「审查分析」页用 ↑/↓ 调整字体优先级。
3. 输入文本（可混排拉丁/阿拉伯/希伯来/中文/Emoji 等），设置语言（如 `en`、
   `zh-Hans`、`ar`、`he`）、书写系统（默认按 UAX #24 推断）、方向、规范化
   方式，并按需添加 OpenType 特性开关（如 `liga off` 观察连字消失）。
4. 点击「开始审查」。结果区显示：
   - 统计条（簇数、覆盖、缺失、首选/回退字体服务数、合并入连字的簇数）；
   - **回退整形尝试**审计轨迹；
   - **字素簇卡片 / 数据表**：绿框=首选字体，紫框=回退字体，红框=字体栈耗尽；
     「连字」角标表示该簇被连字字形合并，「!」表示有警告；
   - 点击任一簇查看详情：码位、实际字体、每个字形的 gid/名称/SVG 轮廓与全部
     定位数据。
5. 勾选「保存为审查记录」后分析，记录即入库；在「审查记录」页可载入、导出或
   删除。载入记录后点击结果区「重新执行并比对」可验证结果一致性。

## 许可门禁使用流程

1. 进入「**许可合规门禁**」标签页，选择场景（🌐 Web / 🖥 桌面 / 🖨 印刷）。
   首次启动每个场景会登记 v1 历史基线并追加激活修正版 **v2**（例如 Web 要求
   `fsType=0/0x0100`、必须具备 nameID 13/14 且 URL 为 https）；`0x0002`
   受限嵌入在三种场景下默认一律拒绝。
2. 「策略版本」区可查看全部不可变历史版本；展开「登记新版本」用策略编辑器
   调整允许的 fsType 掩码、必需字段、禁止正则与冲突规则，填入 **CAS 基准版本**
   后发布——并发陈旧提交会得到 `409`（登记本身仍保留为草稿版本）。
3. 从字体库选择字体组成检查栈，点击「**即时评估**」得到逐条证据与 allow/deny；
   点击「**发布冻结决定**」则原子冻结字体摘要、策略版本与完整清单，
   可填 `request_id` 作为客户端幂等键（同键或同内容指纹重复发布返回同一记录）。
4. 需要例外时，在「豁免」区绑定具体字体、策略版本、检查代码、理由与到期时间；
   过期/撤销/摘要漂移的豁免不会放行。已冻结决定可在页面「复核」或下载 JSON。
5. 「已冻结决定」区列出全部发布记录，可随时**复核**或**导出 JSON 清单**。

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/fonts` | 上传字体（multipart `file`） |
| GET | `/api/fonts` | 已登记字体列表（含摘要、脚本覆盖） |
| GET | `/api/fonts/{sha256}/glyph/{gid}.svg` | 字形轮廓 SVG |
| GET | `/api/fonts/{sha256}/file` | 下载解包后的 sfnt 字体 |
| POST | `/api/analyze?save=&title=` | 执行审查（JSON 请求体） |
| GET/DELETE | `/api/reviews[/{id}]` | 审查记录列表 / 详情 / 删除 |
| POST | `/api/reviews/{id}/rerun` | 固定参数重新整形并逐簇比对 |
| GET | `/api/reviews/{id}/export` | 下载 JSON 报告 |
| GET | `/api/fonts/{sha256}/license-evidence` | 字体许可证据（fsType/name13,14/TTC 成员/解包摘要） |
| GET | `/api/license/policies?scenario=` | 列场景全部不可变策略版本 |
| GET | `/api/license/policies/{scenario}/active` | 当前生效策略 |
| GET | `/api/license/policies/{scenario}/versions/{v}` | 指定不可变版本 |
| POST | `/api/license/policies` | 登记新版本（CAS：`expected_active_version`，可选仅登记不激活） |
| POST | `/api/license/policies/{scenario}/versions/{v}/activate` | CAS 切换生效版本 |
| POST | `/api/license/exemptions` | 登记豁免（字体摘要+版本+代码+理由+到期时间） |
| GET | `/api/license/exemptions` | 豁免查询（场景/版本/字体、含已撤销） |
| POST | `/api/license/exemptions/{id}/revoke` | 撤销豁免 |
| POST | `/api/license/evaluate` | 即时评估（dry-run，不冻结） |
| POST | `/api/license/publish` | 评估并冻结（幂等；`request_id` 或内容指纹命中即返回同一决定） |
| GET | `/api/license/decisions[?scenario=]` | 冻结决定列表 |
| GET | `/api/license/decisions/{id}` | 冻结决定详情（含完整清单） |
| GET | `/api/license/decisions/{id}/export` | 下载冻结 JSON 清单 |
| GET | `/api/health` | 健康检查 |

`/api/analyze` 请求体示例：

```json
{
  "text": "Hello fi! مرحبا 你好",
  "font_stack": ["<首选字体 sha256>", "<回退字体 sha256>"],
  "language": "auto",
  "script": null,
  "direction": "auto",
  "features": [{"tag": "liga", "value": "on"}],
  "normalize": "none"
}
```

`direction` 可选 `auto / ltr / rtl / ttb`；`normalize` 可选 `none / nfc / nfkc`。

### 门禁请求示例

即时评估（不冻结）：

```bash
curl -X POST http://localhost:8000/api/license/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"web","font_stack":["<sha256>"],"policy_version":null}'
```

发布冻结（`policy_version:null` 表示当前生效版本；重复请求靠 `request_id`
或栈内容指纹幂等）：

```bash
curl -X POST http://localhost:8000/api/license/publish \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"web","font_stack":["<sha256>"],
       "request_id":"release-2026-09-22-001"}'
```

登记策略（CAS 基准 = 当前生效版本，首个版本传 0）：

```bash
curl -X POST http://localhost:8000/api/license/policies \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"web","expected_active_version":1,
       "registered_by":"admin","activate":true,
       "policy":{
         "allowed_fstype_masks":[0,256],
         "forbidden_fstype_bits":[2],
         "required_name_fields":["license_description","license_url"],
         "require_https_license_url":true,
         "forbidden_license_patterns":["proprietary[- ]?only"],
         "deny_on_fstype_reserved_bits":true,
         "deny_on_embedding_flags_conflict":true,
         "deny_on_ttc_member_license_mismatch":true,
         "deny_on_incomplete_collection_evidence":true,
         "conflict_rules":[
           {"type":"fstype-flag-pair","flag_a":"preview-print-only",
            "flag_b":"editable","message":"Web 栈不得混用预览与可编辑嵌入"}]}}'
```

登记豁免（`expires_at` 省略表示长期；仍绑定字体摘要与策略版本，撤销即失效）：

```bash
curl -X POST http://localhost:8000/api/license/exemptions \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"web","policy_version":2,"font_digest":"<sha256>",
       "rule_codes":["name-field-bad-url"],"justification":"法务批准 #LIC-1",
       "requested_by":"legal","expires_at":"2026-12-31T23:59:59Z",
       "applies_to_stack_conflict":false}'
```

## 设计说明与边界

### 字形审查

- **回退粒度**：先按 (bidi 级别, 脚本) 切分 shape run，对每个 run 用栈顶字体整体
  整形；HarfBuzz 输出中未覆盖的连续簇区间切出后交给下一个字体重新整形。跨字体
  边界处阿拉伯连写上下文会按各自字体独立整形（与浏览器分段回退行为一致）。
- HarfBuzz buffer 使用 `cluster_level = monotone-characters`，保证输出簇值
  与输入码位可严格对应；字形输入区间按相邻输出簇值计算。
- 整形默认开启 HarfBuzz 全局常见特性（`calt/liga/kern/mark/isol/fina/medi/init`
  等），用户开关在其上叠加。
- 字体的进程内 `hb.Font` 对象带锁串行化访问；磁盘字体以内容 SHA-256 寻址，
  删除字体库元数据不会删除磁盘文件，以保证历史审查记录仍可复核。
- 脚本属性表来自 Unicode UCD（`scripts/Scripts.txt` 等），可用
  `python3 scripts/gen_script_table.py` 重新生成。

### 许可门禁

- **不可变策略版本**：版本号每场景线性自增，策略体登记时严格校验（掩码范围、
  正则可编译、冲突规则类型与 fsType 标签合法），登记后不再改写；生效指针单独
  CAS 切换，陈旧基准返回 `409 Conflict`，但不可变登记事实不回滚。
- **fsType 判定（修正版 v2）**：按 TrueType/OS/2 规范的 fsType embedding
  types 解读——低 4 位（bit1–3，bit0 保留必须为 0）是**互斥的类型枚举**而非
  可叠加位：合法类型值仅 `0`（installable）、`0x0002`（**restricted，受限
  许可，禁止嵌入**）、`0x0004`（preview & print，临时安装不可编辑）、
  `0x0008`（editable）；`0x0001` 为保留值，`0x0006`/`0x000A`/`0x000C`/
  `0x000E` 等组合判为类型互斥冲突。bit8 `0x0100`（no-subsetting）与 bit9
  `0x0200`（bitmap-only）才是可叠加附加位（故 `0x0100` 仍是合法的
  installable 嵌入）；bit0、bit4–7、bit10–15 为保留位（掩码 `0xFEF1`）。
  修正后的三套默认策略均把 `0x0002` 列为禁止位：即使 nameID 13/14 齐备，
  无有效豁免也一律拒绝且证据标为 `restricted`。是否拒绝由策略开关决定。
  v1 基线（把类型值错按位位置解读）作为不可变历史版本保留，仅用于旧冻结
  决定按原版本复核，不影响新评估。
- **存证派生字段以数值 fsType 为准**：升级前登记的字体，存证里的
  `fstype_flags` / `fstype_flag_labels` / `fstype_reserved_bits` 可能仍是旧
  位序口径（`0x0002`→`preview-print-only`、`0x0100`→`bitmap-only`，保留位
  按旧掩码 `0xFEF1`）。评估（`/api/license/evaluate`）、发布
  （`/api/license/publish`）与许可摘要（`/api/fonts/{sha256}/license-evidence`）
  读取存证时，一律在**副本**中按数值 `fsType` 重算全部成员（含 TTC 每个
  集合成员）的这三个派生字段——数值检查、标签冲突规则、逐条证据、新冻结
  清单与页面摘要因此保持一致，不会出现「按修正版拒绝、证据却显示旧标签」
  的矛盾记录。数据库中的原始存证不被改写；此前冻结的决定与其 JSON 导出
  按冻结原貌返回，绝不重算。决定指纹只含成员摘要/容器/解包摘要，不受
  派生标签影响，幂等语义不变。
- **证据完整性**：上传时即从**原始容器**提取证据（TTC 枚举全部成员、
  WOFF/WOFF2 记录解包摘要），与字体元数据一同入库；从磁盘 sfnt 重建的证据
  标记 `reconstructed`。评估/发布前重新对磁盘文件计算 SHA-256 并与登记摘要
  比对，**摘要漂移直接产生 `font-digest-mismatch` 拒绝项**（证据中保留
  claimed/actual 双摘要）。
- **确定性与幂等**：同一策略版本 + 同一栈内容（所有 TTC 成员摘要、容器与
  解包摘要）→ 同一 SHA-256 指纹；评估是纯函数（`evaluated_at` 可注入固定
  时间）。发布以 `(request_id 唯一, fingerprint 唯一)` 双重幂等：同键或同内容
  返回同一冻结记录（201 = 首次，200 = 重复），清单内含策略快照、逐字体证据、
  逐条 findings、考虑/应用的豁免与指纹。
- **豁免语义**：豁免仅在「未撤销 + 未到期 + 场景/版本匹配 + 字体摘要匹配 +
  检查代码匹配」时生效，命中后把拒绝项降级为警告（severity=warn，内嵌豁免
  元数据），无剩余拒绝项才 allow；栈冲突豁免需显式
  `applies_to_stack_conflict` 且摘要为冲突双方之一。
- **边界**：门禁只解读字体二进制内声明的机器可读许可信号（fsType / name 字段 /
  集合一致性），不替代法务对实际授权合同的判断；豁免审批人即登记人，
  应用本身不提供用户体系。

## 项目结构

```
app/
  main.py          FastAPI 路由（字体/分析/记录 + 许可门禁策略/豁免/评估/发布）
  shaper.py        字素簇切分、UBA、HarfBuzz 整形与逐簇回退引擎
  analyzer.py      分析编排与报告结构
  font_loader.py   字体解包/摘要/cmap/UVS、fsType 与 name 许可证据/TTC 成员枚举
  license_gate.py  许可门禁：策略校验、fsType/字段/冲突规则引擎、豁免、指纹
  database.py      SQLite 持久化（审查记录 + 策略/豁免/冻结决定，gzip 快照）
  models.py        请求模型
  script_data.py   由 UCD 生成的码点脚本/Default_Ignorable 属性表
  static/          原生 JS 前端（index.html / style.css / app.js / gate.js）
scripts/           UCD 数据与属性表生成器
data/              运行时生成：SQLite 数据库与字体文件
tests/             单元与 API 端到端测试（含门禁 CAS/豁免/幂等/摘要漂移）
```
