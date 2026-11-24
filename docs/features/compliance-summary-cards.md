# 合规检查页四张汇总卡（统一小字骨架 · 带颜色的小数字指标 · 单列一行一卡）

> `/compliance` 页面顶部四张卡（文档更新检查 / 告警不合规检查 / 红外巡检图片质量 /
> 应急演练合规审查）统一为同一套**小字骨架**，在页面里按**单列 `space-y-4` 一行一卡**
> 排布。每卡只有：图标 + 标题 + 周期 / 一句语境 / **一排带颜色的小数字指标** /
> 底栏元信息 +「查看详情 →」。卡上**没有主指标大字、没有图表、没有按钮**，所有操作与
> 筛选都在点开的整屏看板里。

## 结论（一句话）

一张卡 = 一句语境 + 一排语义色的小数字指标 + 一行元信息，**不铺大字、不画图**；四卡单列
纵向排列，点整卡进对应看板看全量数据。新增第五张卡直接复用 `ComplianceTile`，给
`icon/tone/title/cadence/summary/metrics/meta/onClick` 即可。

## 演进（为什么是小字 + 带色小数字）

- 最早就是「图标 + 标题 + 周期 + 一句摘要 + 底栏元信息」的小字卡，简单、不喧宾夺主。
- 中间试过「主指标大字 + 竖向分布条」，用户否掉。
- 又试过「主指标大字 + 带坐标轴的趋势/构成图，2×2 网格」，用户再次否掉：**「而且还不如
  最初的那种小字统计呢，改回去吧」**，并明确要的是**「一卡片一行的最初带小数字统计的
  那个样式」**。
- 只回成纯一句话小字又不对：用户指出**「我说的是带颜色的小数字指标的那个样式」**，并提示
  可参考服务器上改造前的样式。故本轮＝**保留小字骨架，但摘要区补一排语义色的小数字指标
  chip**（圆点 + 标签 + 加粗着色数字），不回到大数字 / 图表 / 按钮，也不用纯叙述句罗列计数。

## 落地形态

```
┌───────────────────────────────────────────────┐
│ [图标] 标题                            周期     │
│                                               │
│ 一句语境（班次/部门/期间；运行中给进度）        │
│ ● 响应超时 3  ● 未关单 1  ● 未解除 0  ● 重复 2  │  ← 带色小数字 chip
│                                               │
│ ───────────────────────────────────────────── │
│ 底栏元信息（窗口/快照时间/任务数…）  查看详情 → │
└───────────────────────────────────────────────┘
```

- 外层 `Card`：`role="button"` + Enter/Space 可开，`group flex h-full flex-col p-5`，
  `hover:border-primary/40 hover:shadow-md`、focus ring；`muted` 时虚线边框（功能未开启/无产出）。
- **头部**：`size-9` 图标块 + 截断标题 + 右侧 `11px` 周期字。图标块背景用
  `linear-gradient(135deg, color-mix(in oklab, ${accent} 28%, transparent),
  color-mix(in oklab, ${accent} 6%, transparent))`，与 `OverviewPage` 同款。
- **正文槽** `mt-3 space-y-2`：
  - `summary`（可选）：一句语境，`text-sm leading-5 text-muted-foreground`；运行中可放
    紧凑进度条（文档更新检查用 `DocUpdateProgressBar compact`）。
  - `metrics`（可选）：`flex flex-wrap items-center gap-x-2 gap-y-1` 一排 chip。
- **指标 chip**（`ComplianceMetric`）：`text-[11px]` 圆点 `size-1.5 rounded-full bg-current
  opacity-70` 继承文字色 + 标签 + 加粗 `tabular-nums` 数字；浅底 + 文字色由各面板按自己的
  色表传入 `cls`。**0 值照常显示为 0**（口径完整）；**运行中隐藏指标行**（数字是上一轮结果，
  不展示过期值）；无数据/空态不渲染指标行、只留一句语境。
- **底栏** `mt-auto … border-t pt-3`：左元信息（`truncate tabular-nums`），右静态
  「查看详情 →」（`group-hover:text-primary`）。
- 页面容器 `<div className="space-y-4">`：四卡单列纵向排列、间距一致。功能关闭时该卡
  消失，剩余 1~3 张不影响其余卡。

## 每张卡的一句语境 / 指标 / 元信息

| 卡 | 一句语境（`summary`） | 指标 chip（`metrics`） | 底栏元信息（`meta`） |
|----|---------------------|----------------------|--------------------|
| 告警不合规检查 | `{班次} · {日期}`（生成中「正在生成…」/无命中「无命中数据」/未开启说明） | 按检查项注册表逐项：`check_counts[key]`，色用 `visualOf(key).chip` | 班次窗口（或「最近一期生成失败」/「每班 09:00 / 18:00 自动生成」） |
| 文档更新检查 | 运行中紧凑进度条 / `{范围} · {期号}` / 基线或期间缺失提示 | `STATUS_ORDER` 逐档：`verdict.counts[key]`，色用 `STATUS_VISUALS[key].chip` | 期号 + 快照时间（无快照则为范围） |
| 红外巡检图片质量 | 运行中进度 / 检查范围「N 个部门 · 按巡检任务检测」（不点名某个部门，具体部门在看板里选） | `COUNT_ORDER` 逐档（合格/待复核/不合格/失败）：`latest.counts[key]`，色用各项 `chip` | `N 个任务 · 待检测 M`（失败时追加「最近一次检测失败」） |
| 应急演练合规审查 | 「正在审查…」/「已审 N 周」/「正在加载计划清单…」/ 空态引导 | 累计 `severity_counts` 高/中/低，色用 `severityChip(level)` | 最近运行/审查时间（或「每周一自动扫描到点该审的周」） |

数据均来自各 panel 已有的 overview 返回（`latest.check_counts` / `verdict.counts` /
`latest_run.counts` / `weeks[].groups[].severity_counts` 等），**本轮不新增请求、不动后端/API**。

## 组件

- `web/src/components/compliance-tile.tsx`：四卡共用的**小字骨架**，props 为
  `{ icon, tone, title, cadence, summary?, metrics?, meta, muted?, onClick }`；另导出
  `ComplianceMetric`（单个带色小数字 chip）。
- **色表单一来源**（不在卡里另起颜色）：
  - 告警不合规检查：`CompliancePage` 的 `CHECK_VISUALS` / `visualOf(key)`
  - 文档更新检查：`doc-update-status.ts` 的 `STATUS_VISUALS` + `STATUS_ORDER`
    （标签优先 `verdict.labels`，缺省回落 panel 内 `STATUS_FALLBACK`）
  - 红外巡检：`infrared-quality-board.tsx` 的 `COUNT_ORDER`（各项带 `chip`）
  - 应急演练：`drill-audit-board.tsx` 的 `SEVERITY_ORDER` + `severityChip(level)`
- 四个 panel（`doc-update-panel` / `infrared-quality-panel` / `drill-audit-panel` 与
  `CompliancePage` 内的 `CompliancePanel`）各自算好 `summary` + `metrics` + `meta` 后交给
  `ComplianceTile`。

## 不做

- 不在卡上加主指标大字、图表、按钮、筛选（操作与筛选全在看板）。
- 不用 2×2 网格、不做一行大卡的横向两栏。
- 不引图表库、不新增请求；不动看板结构、按钮、轮询间隔与自动刷新。
- 不动 `Card` 基样式与主题 token。

## 复用（新增第五张卡）

1. 在对应 panel 里从已有 overview 数据拼出一句 `summary` 与一排 `metrics`
   （复用该功能已有的色表给 `cls`）；
2. 传 `icon` + `tone` + `title` + `cadence` + `summary` + `metrics` + `meta` + `onClick`；
3. 页面不用动 —— `space-y-4` 容器自动容纳新卡。
