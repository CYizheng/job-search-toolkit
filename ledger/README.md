# ledger · 投递台账

命令行管理求职投递记录，附一个本地网页看板。**Python 纯标准库，无 pip 依赖。**

## 为什么是纯标准库

这类工具的生命周期只有几个月。装依赖的摩擦（venv、pip 源、版本冲突）比功能本身更容易让人放弃写下去。
`argparse / json / sqlite3 / http.server` 足够覆盖需求：单文件存储、命令行操作、本地看板。

## 快速开始

```bash
cd ledger
python qz.py stats           # 漏斗与响应速度
python qz.py ls --due 3      # 3 天内到期
python qz.py serve           # 起网页看板（等价于 python qz_web.py）
```

（Windows 上 `python` 换成你的解释器路径，或直接用 `qz.bat`。）

## 命令

| 命令 | 作用 |
|---|---|
| `qz.py init` | 初始化数据库 |
| `qz.py add --bulk` | 批量粘贴录入，一行一条 |
| `qz.py add --file 新增.txt` | 从文件批量导入 |
| `qz.py add --quick` | 极简模式：一行只写公司（或 `公司\|岗位`），其余用预设补齐 |
| `qz.py ls [--kw 关键词] [--due N] [--all]` | 列表 / 关键词 / N 天内到期 / 全部 |
| `qz.py stats` | 四档分布、漏斗转化、响应速度 |
| `qz.py show <id>` | 单条详情与完整时间线 |
| `qz.py up <id> --to 已挂` | 推进状态（四档：流程中 / 泡池子 / 已挂 / 已放弃） |
| `qz.py set <id> 字段=值 …` | 改任意字段，可一次改多个 |
| `qz.py note <id> "内容"` | 追加一条事件（不改状态） |
| `qz.py next <id> "下一步" --at MM-DD` | 设待办与日期 |
| `qz.py doctor` | 数据体检：待补字段、时间倒置、待办过期 |
| `qz.py stall [--apply]` | 卡住 ≥21 天的记录；`--apply` 落库为「泡池子」 |
| `qz.py baseline` | 响应时长基线：哪几家该催了（只读） |
| `qz.py info [关键词]` | 我的资料：搜索字段 |
| `qz.py info --todo` | 只列待补字段 |
| `qz.py info --set 姓名=张三` | 改某个字段 |
| `qz.py info --copy 手机号` | 把某字段内容复制到剪贴板 |
| `qz.py export --html --xlsx --ics --json` | 导出（`--xlsx` 实际是 CSV，Excel 可直接开） |
| `qz.py import 备份.json` | 从 JSON 备份还原（含事件流） |
| `qz.py open <id>` | 打开该公司的招聘系统页面 |
| `qz.py sites` | 列出预置的招聘系统入口 |
| `qz.py mail` | 从邮箱拉招聘邮件并匹配台账（默认只报告，不写库） |
| `qz.py serve` / `qz_web.py` | 本地网页看板 |

## 数据模型

单文件 SQLite，5 张表。核心是 `applications`（当前状态）＋ `events`（状态变更流水），
**当前状态与历史分离**——这样"某条记录什么时候从流程中变成已挂"是可追溯的，而不是覆盖掉。

`applications` 21 列：

| 组 | 字段 |
|---|---|
| 标识 | `id`、`uid`（去重用的稳定标识） |
| 对象 | `company`、`department`、`position`、`batch`、`channel`、`city`、`referrer` |
| 链接 | `jd_url`、`portal_url` |
| 状态 | `status`、`status_at`、`applied_at` |
| 待办 | `next_action`、`next_action_at`、`priority` |
| 其他 | `salary`、`notes`、`created_at`、`updated_at` |

`status` 只保留四档：`流程中 / 泡池子 / 已挂 / 已放弃`。
刻意不做十几档细状态——细状态需要持续维护，而维护成本一旦上来，台账就会停更。

## 示例数据

`data/qiuzhao.db` 里是 **10 条虚构记录**（星海数据科技、云栖智能、北岸供应链…），
只为让工具能跑起来。真实数据请放本地副本，不要提交回仓库。

重建示例库：删掉 `data/qiuzhao.db`，再跑一次 `python qz.py init`（如该版本无 `init`，用 `add` 逐条补）。

## 资料模板 profile_data.py

148 项字段的结构骨架，覆盖 16 组中文网申表单：

```
① 基本信息   ② 教育·高中   ③ 教育·本科   ④ 教育·硕士   ⑤ 校内职务
⑥⑦⑧⑨ 实习经历 ×4           ⑩ 实践/志愿服务
⑪⑫⑬ 项目经历 ×3           ⑭ 荣誉·技能·其他
⑮ 网申问答（毕业论文/职业规划/自我评价）   ⑯ 严格格式字段（工作经历）
```

所有内容值已清空。两种填法：

```bash
# ① 直接改文件里的「内容」列
# ② 或运行时逐条写库（写进 profile 表，不动源文件）
python qz.py info --set 姓名=张三
python qz.py info --list
```

字段元组格式：`(分组名, 分组序号, 字段名, 内容, 备注, 是否待补(1/0), 组内序号)`。

## 私有数据

`data/mail.json`（邮箱 IMAP 授权码）已在 `.gitignore` 中排除，**永远不要提交**。
`qz.py mail` 相关子命令只从该文件读凭据，代码里不写死任何账号密码。
