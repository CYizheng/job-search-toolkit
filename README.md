# job-search-toolkit

两个自用的求职工具，都刻意做成了**零外部依赖**：

| 子项目 | 是什么 | 技术栈 | 在线演示 |
|---|---|---|---|
| [`mock-interview/`](mock-interview/) | 模拟面试引擎：题库驱动、链式追问、纯录音 + 自动体检 | 原生 JS + HTML/CSS，**零依赖** | [打开](https://USERNAME.github.io/job-search-toolkit/mock-interview/) |
| [`ledger/`](ledger/) | 投递台账：命令行 + 本地网页看板，SQLite 单文件存储 | Python **纯标准库**（无 pip 依赖） | —（本地运行，见下） |

---

## 一、mock-interview：模拟面试引擎

一个纯前端的面试练习器。核心不是"题库"，是**链式追问**：一道主问题后面挂 2–4 层递进追问，逼你在同一个点上连扛三层。

**怎么工作的**

```
questions.js  ──►  app.js  ──►  计时 / 录音 / 逐题推进
（纯数据）        （纯逻辑）      （MediaRecorder）
```

- 题库是一个普通 JS 对象：`window.INTERVIEW = { config, modes, questions }`
- 换题库不用改一行代码，替换 `questions.js` 即可
- 录音只存在浏览器内存与本地，**不上传任何服务器**（示例版没有后端）

**五种模式**：热身 3 题（固定）／分析方法论（随机 3 条链）／项目深挖（随机 2 条链）／行为面（随机 3 条链）／全流程混合。

**示例题库规模**：23 道主问题 + 44 层追问，覆盖行为面、分析方法论、项目深挖、技术基础四类，**不含任何个人经历**，可以直接当模板改成自己的。

**本地跑**

```bash
# 直接双击 index.html 就能用；或起一个静态服务
python -m http.server 8000
# 打开 http://127.0.0.1:8000/mock-interview/
```

> 需要 Chrome / Edge。麦克风权限在 `file://` 下可能被拒，用 `http.server` 更稳。

---

## 二、ledger：投递台账

命令行管理求职投递记录，带一个本地网页看板。315 条真实记录时仍是秒开，因为整个存储就是一个 SQLite 文件。

**为什么是纯标准库**：这类工具的生命周期只有几个月，装依赖的摩擦（venv、pip 源、版本冲突）比功能本身更容易让人放弃。`argparse / json / sqlite3 / http.server` 足够。

**表结构**（21 列的 `applications` + 事件流 `events`）

| 字段组 | 字段 |
|---|---|
| 标识 | `id`、`uid` |
| 对象 | `company`、`department`、`position`、`batch`、`channel`、`city` |
| 链接 | `jd_url`、`portal_url` |
| 状态 | `status`、`status_at`、`applied_at` |
| 待办 | `next_action`、`next_action_at`、`priority` |
| 其他 | `salary`、`notes`、`referrer`、`created_at`、`updated_at` |

**常用命令**

```bash
cd ledger
python qz.py stats                    # 四档分布 + 漏斗 + 响应速度
python qz.py ls --due 3               # 3 天内到期
python qz.py add --bulk               # 批量粘贴录入（一行一条）
python qz.py up 12 --to 已挂          # 推进状态（四档）
python qz.py note 12 "三面结束"        # 追加一条事件（不改状态）
python qz.py doctor                   # 数据体检
python qz.py serve                    # 起网页看板（127.0.0.1）
```

完整命令表见 [`ledger/README.md`](ledger/README.md)。

**示例数据**：`data/qiuzhao.db` 里是 **10 条虚构记录**（星海数据科技、云栖智能…），纯粹为了让工具能跑起来。真实数据请放在本地副本里。

**资料模板**：`profile_data.py` 是 **148 项中文网申表单字段的结构骨架**（16 组：基本信息 / 教育 / 家庭 / 实习 / 项目 / 荣誉 / 网申问答 / 严格格式字段），所有内容值已清空——它是一份可直接复用的字段分类，而不是某个人的资料。

---

## 三、数据安全说明

这个仓库里**没有任何真实个人信息**，是逐项剔除过的：

| 内容 | 处置 |
|---|---|
| 真实投递记录（222 条） | 换成 10 条虚构示例 |
| 个人信息（姓名、手机、邮箱、身份证、家庭） | 全部清空，仅保留字段名 |
| 邮箱 IMAP 授权码 | 从未入库，`.gitignore` 显式排除 `data/mail.json` |
| 面试录音、逐字稿、复盘报告 | 未包含 |
| 个人化题库（559 题） | 换成 23 题通用示例题库 |

如果你要用这套工具，**请 fork 后把自己的数据放在本地**，不要提交回仓库。

---

## 四、License

MIT，见 [LICENSE](LICENSE)。
