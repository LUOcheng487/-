# 航司禁售日报 · 自动汇总推送钉钉

每天早上 **7:30（北京时间）** 把 **前一天** 各平台禁售汇总推送到钉钉群。

## 架构

```
D:\Excel\6个平台xlsx          GitHub 私有仓库
（本地抓取程序写入）              ├── data/          ← 数据（由同步脚本推送）
      │                         ├── report/         ← 汇总+推送脚本
      │  白天电脑开机时           ├── sync/           ← 本地同步脚本
      │  每15分钟自动             └── .github/workflows/
      ▼                                │  每天 23:30 UTC（北京 07:30）云端自动跑
sync_push.py 拷贝到 data/ 并 push      ▼
（无变化零开销）              daily_report.py 读 data/ → 汇总 → 钉钉机器人
```

核心思路（回答两个关键问题）：

1. **不挂电脑怎么定时发**：发送任务跑在 GitHub Actions 云端，早上 7:30 你电脑开不开机都不影响。
2. **云端怎么读本地 Excel**：数据在白天产生时就随同步脚本推到仓库 `data/` 目录（电脑白天反正开着抓数据），
   早上 Actions 直接读仓库里的最新数据。**数据白天随产生随同步，发送完全在云端，两者解耦。**

## 部署步骤（一次性）

### 1. 创建 GitHub 私有仓库并推送代码

在 github.com 网页上新建一个 **Private** 仓库（如 `ban-daily-report`），然后在本目录执行：

```bat
cd /d D:\vsPython\AI留单\ban_daily_report
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
  /TR "\"C:\Users\admin\AppData\Local\Programs\Python\Python312\pythonw.exe\" \"D:\vsPython\AI留单\ban_daily_report\sync\sync_push.py\"" /F
```

立即手动跑一次验证：直接双击执行 `sync\sync_push.py`（或命令行 `python sync\sync_push.py`），
然后到 GitHub 仓库看 `data/` 下是否出现 6 个 xlsx。

### 4. 手动触发一次日报测试

仓库 → Actions → `禁售日报推送` → Run workflow → 填日期如 `2026-09-09` → Run。
钉钉群应收到消息；也可本地先试跑（不发送）：

```bat
cd /d D:\vsPython\AI留单\ban_daily_report
pip install -r report\requirements.txt
python report\daily_report.py --excel-dir "D:\Excel" --date 2026-09-09 --dry-run
```

## 重要说明

- **数据完整性**：本地文件是白天分批写入的。实测飞猪表在次日 7:30 时只有约 **54%** 的前一天记录已入库，
  其余在白天陆续补齐。即 7:30 的日报是"截至发报已入库"的版本。如需完整版：
  打开 workflow 里注释掉的 `20:30` 第二个 cron 即可再收一份晚间补全版；或白天任意时刻手动 Run workflow 补发。
- **同程(1).xlsx 目前是空文件**（结构自动识别已支持，有数据后自动纳入，无需改代码）。
- **美团没有航司列**，其禁售单独显示在"平台级禁售"小节，不参与航司排序。
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
