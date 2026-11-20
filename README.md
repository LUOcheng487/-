# 航司禁售日报 · 自动汇总推送钉钉

每天早上 **09:00（北京时间）** 把 **前一天** 各平台禁售汇总推送到钉钉群。

> **发送条件**：只要仓库 `data/` 里有「报告日期（昨天）」的禁售记录就发；没有记录则当天不发，
> 避免推出空报表。判断只认数据本身，不再要求"上午某个时刻前必须已同步"。详见「发送条件规则」。

## 架构

```
D:\Excel\6个平台xlsx          GitHub 私有仓库
（本地抓取程序写入）              ├── data/          ← 数据（由同步脚本推送）
      │                         ├── report/         ← 汇总+推送脚本
      │  白天电脑开机时           ├── sync/           ← 本地同步脚本
      │  每15分钟自动             └── .github/workflows/
      ▼                                │  每天 1:00 UTC（北京 09:00）云端自动跑
sync_push.py 拷贝到 data/ 并 push      ▼
（无变化零开销）              daily_report.py 读 data/ → 有昨日数据才发 → 钉钉机器人
```

核心思路（回答两个关键问题）：

1. **不挂电脑怎么定时发**：发送任务跑在 GitHub Actions 云端，上午 09:00 你电脑开不开机都不影响。
2. **云端怎么读本地 Excel**：数据在白天产生时就随同步脚本推到仓库 `data/` 目录（电脑白天反正开着抓数据），
   09:00 时 Actions 直接读仓库里的最新数据。**数据白天随产生随同步，发送完全在云端，两者解耦。**

## 部署步骤（一次性）

### 1. 创建 GitHub 私有仓库并推送代码

在 github.com 网页上新建一个 **Private** 仓库（如 `ban-daily-report`），然后在本目录执行：

```bat
cd /d D:\vsPython\禁售消息自动推送
git remote add origin https://github.com/<你的用户名>/ban-daily-report.git
git push -u origin main
```

> 需要本机 git 已登录 GitHub（你之前用过 Actions，凭据管理器里应该已有；首次 push 弹窗登录一次即可）。

### 2. 配置仓库 Secrets

仓库页面 → Settings → Secrets and variables → Actions → New repository secret，添加：

| Secret 名 | 值 |
|---|---|
| `DING_WEBHOOK` | 钉钉机器人 Webhook 地址（`https://oapi.dingtalk.com/robot/send?access_token=xxx`） |
| `DING_SECRET` | 机器人的加签密钥 SEC 开头（若机器人用的是关键词安全设置，此 Secret 留空不建） |

### 3. 注册本地数据同步任务（每15分钟）

管理员 CMD 执行（路径按实际 Python 位置调整，`pythonw` 静默无窗口）：

```bat
schtasks /Create /TN "禁售数据同步GitHub" /SC MINUTE /MO 15 ^
  /TR "\"C:\Users\admin\AppData\Local\Programs\Python\Python312\pythonw.exe\" \"D:\vsPython\禁售消息自动推送\sync\sync_push.py\"" /F
```

立即手动跑一次验证：直接双击执行 `sync\sync_push.py`（或命令行 `python sync\sync_push.py`），
然后到 GitHub 仓库看 `data/` 下是否出现 6 个 xlsx。

### 4. 手动触发一次日报测试

仓库 → Actions → `禁售日报推送` → Run workflow → 填日期如 `2026-09-09` → Run。
钉钉群应收到消息；也可本地先试跑（不发送）：

```bat
cd /d D:\vsPython\禁售消息自动推送
pip install -r report\requirements.txt
python report\daily_report.py --excel-dir "D:\Excel" --date 2026-09-09 --dry-run
```

## 发送条件规则

发钉钉前只检查一件事，不通过则**当天不发**（进程返回 0，Actions 显示成功但不出消息）：

| 检查 | 通过条件 | 不通过时 |
|---|---|---|
| 报告日是否有数据 | `data/` 里存在报告日期（默认昨天）的禁售记录 | "报告日期 … 在 data 中没有禁售记录，当天不发送钉钉" |

- 默认只认数据本身：有昨日记录就发，没有就不发，不要求"上午某个时刻前必须已同步"。
- 如需恢复"同步时间闸门"（例如要求上午 09:00 前必须已同步，否则当天不发），加参数
  `--gate-time 09:00` 或设环境变量 `GATE_TIME=09:00` 即可；`--gate-time ""`（默认）为关闭。
- **手动补发**：Actions → Run workflow → `force` 填 `1`（或本地 `--force`），可跳过空数据检查强制发送。
- 本地调试 `--dry-run` 不发送，但会打印报告日条数，方便确认今天到底有没有数据。

## 重要说明

- **数据完整性**：本地文件是白天分批写入的，早上 09:00 时当天记录可能尚未补全
  （实测飞猪表在次日 7:30 仅约 **54%** 的前一天记录已入库）。09:00 发报反映的是"发报时已入库的部分"，
  想更全可延后发报时间，或事后用 `force=1` 手动补发一次。
- **同程(1).xlsx 目前是空文件**（结构自动识别已支持，有数据后自动纳入，无需改代码）。
- **美团没有航司列**，其禁售单独显示在"平台级禁售"小节，不参与航司排序。
- 日报明细按“航司 → 平台 → 店铺”展开：飞猪使用“店铺”，美团使用“站点”，去哪儿使用“域名”，智行/携程使用“票台”；缺失或 `*` 显示为“未指定店铺”。
- 智行/携程表中 `是否有效=0` 的记录目前**默认也计入**（它是"已发生过的禁售"事实记录）；如需只统计有效记录，在适配器里过滤该列即可。
- 6 个文件每天合计几十 KB，私有仓库完全够用；每次同步只在有变化时产生一个小 commit。

## 文件结构

```
├── config.json                     # 平台/文件映射（改文件名只动这里）
├── report/daily_report.py          # 汇总 + 钉钉推送（可独立本地运行）
├── report/requirements.txt
├── sync/sync_push.py               # 本地→GitHub 数据同步
├── .github/workflows/daily-report.yml
└── data/                           # 同步后的数据（自动生成）
```

## 新增平台 / 改文件名

在 `config.json` 的 `platforms` 里加一行即可；表结构若与现有4种一致（`schema` 填 `auto` 自动识别），
不一致则在 `daily_report.py` 的 `ADAPTERS` 里加一个适配函数。
