# 北师大羽毛球场空位监控 🏸

> **已适配北师大智慧场馆接口** `POST /www/tycg/app/getYdxcd`（`config.json` 里的 `parser` 已填好）：
> `data.sjkcds[].sjklist[]` 是「场地 × 时段」，`ostatus == 0` 表示可预约。
> 系统每天 **07:30** 放出第四天的场地（接口返回 `kfsj: 07:30:00`），所以 07:28–07:45 每 10 秒查一次；
> 7:30 之前不查第四天，免得把还没放出的场当成空位。凌晨每 5 分钟查一次，同时保持登录状态。
> 只依赖 Cookie 登录：`BNU_REQUEST_CURL` 里要包含 `dd.SessionId`、`dd.CookieId`。

定时查询北师大智慧场馆（`tycg.bnu.edu.cn`）的羽毛球场。一发现空余场地（包括别人退出来的）就发邮件给你，你再自己去抢。

- **只看不订**：不会自动下单，不绕过验证码。
- **当前规则**：只看每天 10:00–22:00、羽1–羽8；同一场地连续空出 ≥2 场（比如 10-12 点）时邮件标题标「🔥 特别提醒」。
- **只提醒新空位**：第一次运行时，把当前所有空位汇总成一封邮件。之后只有新出现的空位才提醒；同一个空位不会反复发。如果一个空位被订走又被退出来，会再提醒一次。
- **7:30 放场冲刺**：北京时间 07:28–07:45 每 10 秒查一次，白天每 60 秒，凌晨每 5 分钟。
- **登录失效会提醒**：Cookie 过期时发邮件提醒你更新（同一次失效只发一封）；连续查询失败也会告警。
- 只依赖 Python 3.8+ 标准库，可以跑在 GitHub Actions 上，也可以跑在自己电脑上。

> 架构思路参考了 [tennis-monitor](https://github.com/siyuwan0218-boop/tennis-monitor)：GitHub Actions 每次触发后长时间轮询、前后两班接力，并用缓存保存去重状态。由于那个仓库没有开源许可证，这里的代码是独立实现的。

---

## 0. 先在本地看效果（模拟接口，不需要登录）

```bash
python demo/demo_server.py &                        # 启动一个假的场馆接口
python monitor.py -c demo/config.demo.json --once   # 第一轮：列出所有空位
python monitor.py -c demo/config.demo.json --loop   # 持续监控，模拟接口会随机「退场」
```

演示配置开了 `dry_run`，邮件内容只打印在终端里，不会真的发出去。

## 1. 抓取查询请求（关键一步）

1. 用 Chrome / Edge 打开 <https://tycg.bnu.edu.cn/www/dd/vue/spa/zhcg#/>，登录后进入羽毛球场的预约页面。
2. 按 `F12` → 打开「网络 / Network」→ 选「Fetch/XHR」→ 点清空 🚫。
3. 在页面上切换到**第四天**的日期，列表里会出现新的请求。逐个点开看「预览 / Preview」，找到返回内容里有场地和时段状态的那一个。
4. 在这个请求上右键 →「复制」→「**复制为 cURL (bash)**」（不要选 cmd 或 PowerShell）。
5. 把复制的内容粘贴到本目录的 `request.txt` 里（本地运行时用；这个文件已被 gitignore，不会提交）。

`request.txt` 里有你的登录凭证，**不要发给别人，也不要提交到仓库**。

## 2. 填写 `config.json`

| 字段 | 含义 |
|---|---|
| `date_in_request` | 你抓请求时所选的那个日期，原样写进来（比如 `2026-10-14`）。程序会把它依次替换成今天和之后 3 天 |
| `date_format` | 日期格式。请求里是 `20261014` 就写 `%Y%m%d` |
| `day_offsets` | 查哪几天，`[0,1,2,3]` 表示今天到第四天 |
| `time_range` | 只看这个时间范围内的场次，如 `["10:00", "22:00"]`（场次的开始和结束都要在范围内） |
| `court_regex` | 只看场地名匹配这个正则的场地，如 `"^羽[1-8]$"` 只看羽1–羽8 |
| `streak_min` | 同一场地连续空出几场就发「🔥 特别提醒」，默认 2（≥2 小时） |
| `include_times` | 只关心的时段，比如 `["18:", "19:", "20:"]`；留空表示全部 |
| `include_courts` | 只关心的场地（按名称包含匹配）；留空表示全部 |
| `daily_report_at` | 比如 `"22:00"`：每天这个时间发一封「监控正常」的日报；留空不发 |
| `parser` | 怎么从返回的 JSON 里找出空位，见下文 |

然后运行：

```bash
python monitor.py --inspect
```

它会打印接口返回内容，并列出可能是场地列表的路径。按输出填写 `parser`：

```jsonc
"parser": {
  "ok_when":   {"field": "code", "in": [200]},   // 接口成功的标志；失败且提示「登录」等字样时判定为登录失效
  "list_path": "data.courts.*.slots.*",          // 每个「场地×时段」格子所在的位置，* 表示遍历列表的每一项
  "free_when": {"field": "status", "in": [0]},   // 什么情况下算空闲（也可以用 not_in / equals / gt / regex）
  "fields":    {"time": "time", "court": "../name"}  // 邮件里显示的字段；../ 表示从外层对象取
}
```

如果接口返回的不是 JSON 而是 HTML，可以用正则模式：
`"parser": {"mode": "regex", "pattern": "<正则，用 (?P<time>...) (?P<court>...) 命名分组>"}`。

再运行一次 `--inspect`，确认最后打印的空位和页面上看到的一致。

## 3. 配置邮箱

以 QQ 邮箱为例：网页版 QQ 邮箱 → 设置 → 账号 → 开启「POP3/SMTP 服务」，得到一个**授权码**（不是 QQ 密码）。

| 邮箱 | host | port |
|---|---|---|
| QQ 邮箱 | smtp.qq.com | 465 |
| 163 邮箱 | smtp.163.com | 465 |
| Gmail | smtp.gmail.com | 465（需要应用专用密码） |

本地运行时，通过环境变量提供：

```bash
export SMTP_USER=123456@qq.com SMTP_PASSWORD=授权码 MAIL_TO=你的收件邮箱
python monitor.py --test-email
```

## 4. 部署到 GitHub Actions

1. **新建一个公开仓库**（例如 `bnu-badminton-monitor`），把本目录的所有文件（包括 `.github/`）放到仓库**根目录**。
   - 公开仓库的 Actions 分钟数不限；私有仓库每月只有 2000 分钟，不够全天轮询。
   - 登录凭证和邮箱授权码都放在 Secrets 里，公开仓库也看不到。
2. 仓库 Settings → Secrets and variables → Actions → New repository secret，添加：

   | Secret | 值 |
   |---|---|
   | `BNU_REQUEST_CURL` | 第 1 步复制的整段 cURL |
   | `SMTP_HOST` | `smtp.qq.com` |
   | `SMTP_PORT` | `465` |
   | `SMTP_USER` | 发件邮箱 |
   | `SMTP_PASSWORD` | 邮箱授权码 |
   | `MAIL_TO` | 收件邮箱（多个用逗号分隔，不填则发给自己） |

3. Actions 页 → `badminton-monitor` → Run workflow：
   - 先选 `test-email`，确认能收到邮件；
   - 再选 `check`，在日志里确认能查到场地。**这一步同时验证 GitHub 的海外服务器能不能访问学校网站**；
   - 最后选 `loop`，或者等定时任务自动开始。
   - `fresh-loop`：清空「已提醒过的空位」记录后开始监控，当前所有空位会重新发一封邮件（比如改了收件地址之后用）。

## 5. 更新登录状态

收到「登录已失效」邮件时：重新登录预约系统，按第 1 步重新复制 cURL，然后更新 Secret `BNU_REQUEST_CURL`（本地运行则覆盖 `request.txt`）。下一班运行会自动用新的凭证。

## 注意事项

- **海外 IP**：GitHub Actions 的服务器在海外。如果学校网站屏蔽境外访问，第 4 步的 `check` 会报错，这时改到校内电脑或国内云服务器上用 `--loop` 运行即可。
- **登录有效期**：Cookie 或 Token 能用多久由学校系统决定。持续轮询可能会让会话一直保持有效，也可能每天都要更新，需要实际跑几天才知道。
- **频率**：默认每分钟查一次、放场时每 10 秒查一次，每次查 4 天，对学校服务器的压力很小。请不要把间隔改得太短。
- 放场时间以后如果变了，同时修改 `release_time` 和 `burst_windows`。

## 命令一览

```
python monitor.py --test-email     发测试邮件
python monitor.py --inspect        打印接口返回，帮你填 parser
python monitor.py --check          查一轮，只打印不发邮件
python monitor.py --once           查一轮，有新空位就发邮件（适合 cron）
python monitor.py --loop           常驻轮询
python monitor.py --ci-loop 19800  GitHub Actions 长跑模式
python -m unittest test_monitor    运行测试
```
