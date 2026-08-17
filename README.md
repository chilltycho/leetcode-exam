# LeetCode 周考自动化工具（leetcode.cn）

公司每周五编程考试的自动化工具：自动抽题（不与历史重复）、自动判分（考试窗口内是否 AC）、自动生成成绩报告。

```
抽题 pick ──→ 员工用自己账号在考试窗口内提交 ──→ 判分 score ──→ 报告 report
（周五 09:00）                                    （截止后运行）
```

## 依赖

- Python 3.9+（内置 `sqlite3`、`zoneinfo`）
- `pip install requests`

## 快速开始

1. **数据源文件**（本目录下，直接维护即可，程序每次运行自动读取）：

   - `users.md` —— 员工名单与 LeetCode 账号主页，**每行一条**：`姓名 https://leetcode.cn/u/<账号>/`（姓名与 URL 之间用空格/逗号分隔均可）。新增/离职员工直接增删行即可。
   - `used_problems.txt` —— 历次已考题目编号（题号，逗号/顿号/换行分隔）。抽题时会自动排除这些题号，**后续不应重复**。

2. **配置考试参数**（`config.json`）：

   ```json
   {
     "exam": {
       "day_of_week": 4,
       "start_time": "09:00",
       "end_time": "17:00",
       "timezone": "Asia/Shanghai"
     },
     "difficulties": { "EASY": 2, "MEDIUM": 2, "HARD": 1 }
   }
   ```

3. **校验账号**：`python exam_tool.py verify`（会逐个检查 users.md 中的账号是否存在）

4. **每周五流程**：

   ```bash
   # 周五 09:00：抽题（默认取当天日期），输出 5 道题链接，发给员工
   python exam_tool.py pick

   # 截止时间后：判分（--force 表示截止未到也强制统计）
   python exam_tool.py score

   # 生成报告（reports/日期_成绩.csv 与 .md，同时打印到终端）
   python exam_tool.py report
   ```

4. **自动定时**：推荐直接使用仓库内置的 **GitHub Actions**（见下文「部署」），无需自建服务器；本地服务器则用 cron：

   ```cron
   # 每周五 09:00 抽题并写入日志
   0 9  * * 5  cd /path/to/leetcode-exam && python3 exam_tool.py pick >> run.log 2>&1
   # 每周五 17:30 判分 + 生成报告
   30 17 * * 5 cd /path/to/leetcode-exam && python3 exam_tool.py score >> run.log 2>&1 && python3 exam_tool.py report >> run.log 2>&1
   ```

## 部署：GitHub Actions + 网页看板 + 钉钉通知

仓库已内置 `.github/workflows/pick.yml` 与 `score.yml`，推送后即可使用：

| 触发 | 北京时间 | 动作 |
|---|---|---|
| `pick.yml` | 每周五 09:00 | 抽题 → 提交 `exams.db`（保证不重复）→ 钉钉发题目 |
| `score.yml` | 每周五 17:30 | 判分 → 生成报告 → 更新网页看板 → 部署 Pages → 钉钉发成绩 |

两个 workflow 都支持 `workflow_dispatch`（Actions 页面手动触发），首次部署可手动跑一遍验证。

### 一次性配置

1. **推送代码**：把本仓库推到 GitHub（`leetcode-exam/` 与 `.github/workflows/` 一并提交）。
2. **启用 GitHub Pages**：仓库 Settings → Pages → Source 选 **GitHub Actions**（之后由 `score.yml` 自动部署，无需再操作）。
3. **配置 Secrets**：仓库 Settings → Secrets and variables → Actions，添加：

   | Secret | 必填 | 说明 |
   |---|---|---|
   | `DINGTALK_WEBHOOK` | 推荐 | 钉钉机器人 Webhook 地址（见下） |
   | `DINGTALK_SECRET` | 按需 | 机器人开启「加签」时的密钥 |
   | `LEETCODE_SESSION` | 按需 | 浏览器登录 leetcode.cn 后的 Cookie（见下） |
   | `LEETCODE_CSRF` | 按需 | 同上，`csrftoken` 值 |

4. **钉钉机器人**：钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义机器人 → 复制 Webhook。若开启「加签」，把密钥填入 `DINGTALK_SECRET`。机器人安全设置建议至少保留「加签」或自定义关键词（如「周考」）。
5. 打开 Actions 页面，手动运行一次两个 workflow 验证。

### 关于 LEETCODE_SESSION（重要）

GitHub Actions 运行在**机房 IP**，leetcode.cn 的 Cloudflare 风控可能返回 403。脚本内置 4 次重试 + 可选 Cookie：
登录 leetcode.cn 网页版 → 浏览器开发者工具 → Application → Cookies → 复制 `LEETCODE_SESSION` 和 `csrftoken` 填入 Secrets 即可显著提高稳定性（Cookie 约两周过期，需更新）。若仍频繁失败，建议改用自托管 runner（公司内网 IP）。

### 网页看板

- 判分后 `score.yml` 会自动运行 `dashboard/generate_dashboard.py`，从 `exams.db` 生成纯静态 `dashboard/index.html`（员工累计统计卡片 + 每场考试题目/成绩表/通过率），并部署到 GitHub Pages。
- 看板地址形如 `https://<用户名>.github.io/<仓库名>/`（如 `https://chilltycho.github.io/leetcode-exam/`）。配置 `config.json` 的 `notify.dashboard_url` 后，钉钉成绩通知末尾会附带看板链接。
- 本地预览：`python3 dashboard/generate_dashboard.py` 后直接打开生成的 `index.html`。

### 本地手动通知

不发全量流程、只想手动推送通知时：

```bash
DINGTALK_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=xxx" \
DINGTALK_SECRET="可选加签密钥" \
python3 exam_tool.py notify --type problems   # 或 results，加 --dry-run 只预览不发送
```

## 命令参考

| 命令 | 说明 |
|---|---|
| `pick [--date YYYY-MM-DD] [--force]` | 抽题：2 easy + 2 medium + 1 hard，自动排除历次已用题目。`--force` 用于同日期已抽过时强制重抽 |
| `score [--date YYYY-MM-DD] [--force]` | 判分：对每位员工拉取最近 AC 提交，判断 5 题是否在考试窗口 `[开始, 截止]` 内 AC。未到截止时间会拒绝执行（除非 `--force`） |
| `report [--date YYYY-MM-DD]` | 生成 `reports/日期_成绩.csv`（Excel 可直接打开）与 Markdown 报告 |
| `notify --type problems\|results [--date] [--dry-run]` | 发送钉钉机器人通知（考题 / 成绩），webhook 取环境变量 `DINGTALK_WEBHOOK`（其次 config.json） |
| `verify` | 校验员工名单（users.md）中所有账号是否存在 |
| `add-employee --name 姓名 --slug 用户名` | 向 config.json 追加员工（仅当不使用 users.md 时） |

`--date` 缺省时：`pick` 取今天，`score`/`report` 取数据库中最近一次考试。

## 判分规则

- 每道题只记 **通过 / 未通过**（AC / 未 AC）。
- "通过" = 员工账号在 `[考试开始, 考试截止]` 时间窗口内存在该题的 AC 提交。
- 之前就 AC 过、但考试窗口内没有再提交的题目，**不计为通过**（考试要求窗口内解决）。
- 同一题在窗口内多次 AC 取最早一次，不重复计分。

## 数据与接口说明

- **员工名单**：`users.md`（程序每次运行动态解析，改文件即生效）；`config.json` 的 `employees` 仅作回退。
- **已考题目**：`used_problems.txt`（题号列表）+ `exams.db` 中本工具历次抽题记录，两者合并排除，保证不重复。
- 数据存于 `exams.db`（SQLite，自动创建）：考试记录、每场题目、每人每题的 AC 结果。
- 题库缓存于 `problems_cache.json`（7 天过期，删除该文件即可强制刷新）。
- 使用 leetcode.cn 匿名 GraphQL 接口（`/graphql/` 拉题、`/graphql/noj-go/` 拉 AC 提交），无需登录；可选环境变量 `LEETCODE_SESSION`/`LEETCODE_CSRF` 携带浏览器 Cookie 以通过机房 IP 的 Cloudflare 风控。

## 已知限制与建议

1. **提交记录只有最近约 15 条 AC**：`recentACSubmissions` 匿名接口返回上限约 15 条最近 AC。建议**考试截止后尽快判分**（cron 已按截止后 30 分钟配置），避免员工截止后大量做题把考试提交挤出窗口。
2. **匿名接口偶发 Cloudflare 拦截**：脚本内置 4 次指数退避重试；若频繁 403，可把 `config` 所在机器 IP 换为住宅 IP，或在请求头补充 `LEETCODE_SESSION` Cookie。
3. **员工需在考试当天使用自己的账号**提交到对应题目标题下（用 `pick` 输出的链接直接打开）。
4. 抽题默认排除**付费题**（员工可能无会员）。
5. 题库用尽（每个难度未使用题少于需求）时会自动从已用题目中补充，并打印警告。

## 目录结构

```
leetcode-exam/              # 本仓库根目录
├── exam_tool.py            # 主程序（pick/score/report/notify/verify/add-employee）
├── config.json             # 考试参数与通知配置（员工名单见 users.md）
├── users.md                # 员工名单 + LeetCode 账号主页（数据源，直接维护）
├── used_problems.txt       # 历次已考题目编号（数据源，抽题时排除）
├── dashboard/
│   ├── generate_dashboard.py   # 从 exams.db 生成网页看板 index.html
│   └── index.html              # 看板产物（提交到仓库供 Pages 部署）
├── exams.db                # 数据库（自动生成，已 gitignore）
├── problems_cache.json     # 题库缓存（自动生成，已 gitignore）
└── reports/                # 成绩报告输出（已 gitignore）

.github/workflows/
├── pick.yml                # 周五 09:00 抽题 + 通知
└── score.yml               # 周五 17:30 判分 + 看板 + 通知 + 部署 Pages
```
