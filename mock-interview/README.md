# mock-interview · 模拟面试引擎

纯前端。打开 `index.html` 即可用，没有构建步骤、没有依赖、没有后端。

## 它解决什么

看面经和把嘴张开是两件事。这个工具的假设是：**你缺的不是题目，是在同一个点上被连续追问三层的经验。**

所以数据结构里，每道主问题都挂着一条 `chain`（追问链）。前端按 `chainDepth` 逐层往下推，答完一层才出下一层。

## 三种文件，各管一件事

| 文件 | 职责 | 换它会影响什么 |
|---|---|---|
| `questions.js` | **纯数据**：config / modes / questions | 换题库不用改任何代码 |
| `app.js` | **纯逻辑**：计时、录音、推进队列、告警统计 | 改交互才动它 |
| `style.css` | 样式 | — |

## 题库格式

```js
window.INTERVIEW = {
  config: {
    prepSec: 10,            // 每题准备秒数
    answerSec: 90,          // 每题作答秒数
    followupPrepSec: 5,
    followupAnswerSec: 60,
    chainDepth: 3,          // 全局默认追问层数
    agency: "显示在页头的题库标识"
  },
  modes: {
    // ① 固定题目：列出 id
    warm:     { label: "① 热身", ids: ["B1", "A1"], chainDepth: 2, desc: "…" },
    // ② 按赛道随机抽：tracks + pick
    analysis: { label: "② 方法论", tracks: ["analysis"], pick: 3, chainDepth: 3, desc: "…" },
    // ③ 分段混合：每段可各设层数
    full:     { label: "③ 全流程", chainDepth: 2,
                mix: [ { tracks: ["behavior"], pick: 1, chainDepth: 2 },
                       { tracks: ["project"],  pick: 1, chainDepth: 3 } ] }
  },
  questions: [
    {
      id: "A1",
      label: "分析｜核心指标下跌",   // 进度条上显示的名字
      type: "方法论 · 异动归因",      // 副标题
      track: "analysis",             // 归属赛道，modes 按它抽题
      question: "某个核心指标昨天突然跌了 20%，你怎么查？",
      hint: "参考答法框架…",          // 作答前/后可看的提示
      check: [],                     // 作答中的提醒（字符串数组）
      chain: [
        { q: "怎么判断是数据问题还是业务问题？", hint: "…", check: ["⚠ …"] },
        { q: "如果拆完发现每个渠道都在跌呢？",   hint: "…", check: [] }
      ]
    }
  ]
};
```

要点：

- **`chain` 长度可以大于 `chainDepth`**。工具只取前 N 层，剩下的留着，改 `chainDepth` 就能加深度。
- **`check` 是"提醒"**，不是答案。写成 `"⚠ 答'我在判违规'就输了"` 这种，只在作答界面旁边显示。
- **同场不重复出题**：`resolvePicks()` 用一个 `used` 表去重，所以 `mix` 里两段赛道重叠也不会撞题。

## 示例题库

23 道主问题 + 44 层追问，四类赛道：

| 赛道 | 主问题 | 内容 |
|---|---|---|
| `behavior` | 6 | 自我介绍 / 动机 / 失败复盘 / 冲突协作 / 抗压排序 / 缺点 |
| `analysis` | 8 | 指标异动归因 / 指标体系 / A-B 实验 / 留存 / 口径对齐 / 漏斗 / 费米估算 / 值不值得做 |
| `project` | 5 | 项目开场 / 技术选型 / 数据质量 / 结果可信度 / AI 工具产出 |
| `sql` | 4 | 窗口函数三兄弟 / LEFT JOIN 计数 / 慢查询 / p 值 |

换成自己的题库：直接改 `questions.js`，或写个脚本从 Markdown 题库生成（本项目的完整版就是用生成器从 5 份 Markdown 抽出来的，结构完全一致）。

## 本地运行

```bash
# 方式一：直接双击 index.html
# 方式二（推荐，麦克风权限更稳）
python -m http.server 8000
# 打开 http://127.0.0.1:8000/mock-interview/
```

浏览器需要支持 `MediaRecorder` 与麦克风授权，Chrome / Edge 均可。

## 这个示例版缺了什么

完整版还做了两件需要本地后端的事，**示例版刻意去掉**了：

| 能力 | 完整版怎么做的 | 为什么没带进示例版 |
|---|---|---|
| 录音落盘 | 一个 130 行的 Node `http.server`，POST 到 `/api/upload` | 会引入后端，破坏"零依赖" |
| 自动体检 | 本地 `faster-whisper` 转写 + 脚本分析语速/填充词/覆盖度 | 模型几百 MB，且需要麦克风真实入声 |

前端留下了兼容位：`app.js` 里两处 `fetch('/api/…')` 都带 `.catch(() => {})`，所以没有后端时静默降级，不影响出题与计时。
