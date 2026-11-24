# 工具目录（智能体页内的小窗）

> 2026-11-09 上线，2026-11-10 改为智能体页内的小窗：侧边栏「工作台 → 智能体」
> （`/agent-skills`）工具栏的「工具」按钮打开只读工具目录，权限 `ai:agent`。
> 旧地址 `/agent-tools` 与 `/agent` 均跳转到 `/agent-skills`（独立的工具中心页已停用）。
> 后端 `api/v1/tools.py` + `service/query/tool_catalog.py`，前端
> `web/src/pages/AgentSkillsPage.tsx` + `web/src/components/agent-tools-dialog.tsx` + `web/src/api/tools.ts`。

## 页面口径

工具目录回答一个问题：**这个智能体现在能做什么**。

- 入口：智能体页 tab 栏最左侧（「全部」之前）的「工具」按钮，点开是一个小窗（`max-h-[85vh]`、
  遮罩点击、Esc 或右上角 ✕ 关闭）。进入智能体页即预取工具清单（`prefetchTools()`，
  模块级缓存 + 请求去重，路由切走再回来/严格模式双挂载都不重复请求），打开小窗直接展示、
  不闪加载态；预取失败时打开小窗才重试并显示错误。
- 窗内置搜索框（工具中文标题 / 英文工具名 / 分类 / 简短描述），并显示「共 / 命中 N 个工具」。
- 按来源分组：平台分组（综合管理平台 / 动力环境数据监控 / 联网 / 页面操作）+ 内置分组
  （基础能力 / 数据分析 / 技能管理 / 任务清单 / 文件上传 / 历史回读 / 事件统计 / 定时任务 /
  事件告警关联 / 文档更新检查 / 讲解视频 / 飞书消息）。**技能不在这里列** —— 技能只在智能体页
  （`/agent-skills`）展示，工具目录不重复。
- **每个工具一行长条**：图标块 + 分类胶囊（`category_label`，内置工具为空时显示「内置」）+
  中文标题 + `summary` 一句话描述（最多两行）+ 行尾可见性文案（常驻可用 / 按需检索 / 按任务模式）。
  完整 `description` 放在整行的 `title` 提示里，不在页面展开。
- 搜索命中的工具照常按组展示，空分组自动隐藏；**没有详情弹窗** —— 工具没有 `SKILL.md`
  那样的正文。
- 不提供启停：工具可用性由部署开关与角色权限决定（见下节）。

## 为什么只读、不提供启停

工具能不能用由三层决定，页面不介入：

1. **部署开关**（`MGMT_ENABLED` / `DCIM_ENABLED` / `AGENT_ANALYSIS_ENABLED` / …）——
   关闭的工具根本不会注册；
2. **RBAC 角色权限**（`ai:agent` / `ai:agent:*` / `ai:agent:<tool>`，见
   `core/agent/middlewares/guardrail.py`）；
3. **运行时可见性**（`tool_search` 按会话 promote；模式门控平台只在特定任务模式放开）。

而 Agent 图是**按配置指纹缓存共享**的（`service/agent/agent_service.py::_ensure_agent`），
按用户加减工具会让图无法复用、只能按用户重建 —— 代价远大于收益。技能能启停是因为技能
状态存在 Mongo、在运行时两处判断，不需要重建图；工具没有等价的运行时开关。

## 数据来源

### 1. 平台能力（`platform_groups`）

- 按 `cap.id.split('.')[0]` 分组，保持注册顺序；**跳过 skill 平台** —— 技能只在智能体页展示，
  工具目录不重复列（技能能力仍在全局注册表里）。
- 每项 `{name, title, description, category_label, domain_label, required_perm, visibility, mode_gated}`，
  其中 `name = capability_to_tool_name(id)`（`.` → `__`），中文标签复用
  `manifest.category_display_name` / `domain_display_name`。
- 分组头 `{label: platform_display_name, description: platform_definition}`。
- **可见性口径**与 `agent_service._rebuild` 一致（三选一）：
  - `mode_gated`：模式门控平台（目前只有 web），只在联网调研模式 / 联网搜索开关下可见；
  - `always`：构建时**不进 deferred** 的工具 —— 公开父技能（`skill__chart` 等）与
    `BROWSER_EXPLORE_ENABLED` 下的交互浏览 7 件套；
  - `on_demand`：其余平台能力，需要模型先 `tool_search` promote。

### 2. 内置工具（`_BUILTIN_GROUPS`）

代码里直接装配的工具没有 `Capability` 元数据，用**声明式表**枚举：
每组 `{key, label, description, switch, entries, defer}`，`entries` 是
`(模块, 属性, 是否工厂)`，运行时 `importlib` 动态取 `.name` / `.description`；
中文标题来自 `TOOL_TITLES`（缺失回退工具名）。`defer=True` 的组与平台能力走**同一套**
deferred 机制：默认不常驻 schema，`tool_search` 检索到才加载，页面可见性显示「按需检索」。

| 组 | 开关 | 可见性 | 工具 |
|---|---|---|---|
| 基础能力 | 常开 | 常驻 | rag_knowledge_search / get_current_time / send_notification |
| 数据分析 | `AGENT_ANALYSIS_ENABLED` | 常驻 | build_analysis_tools()（load / exec / row_detail + 6 个文件工具） |
| 技能管理 | 常开 | 按需 | skill_manage / stage_skill_script |
| 任务清单 | 常开 | 常驻 | write_todos |
| 文件上传 | 常开 | 常驻 | build_upload_tools() 5 个 |
| 历史回读 | 常开 | 常驻 | build_history_tools() 2 个 |
| 事件统计 | 常开 | 按需 | incident_report / incident_progress / group_message_search |
| 日常变更统计 | 常开 | 按需 | change_notice_report |
| 定时任务 | `SCHEDULED_TASKS_ENABLED` | 按需 | schedule_task |
| 事件告警关联 | `DCIM_ENABLED` | 按需 | event_alarm_link |
| 文档更新检查 | `DOC_UPDATE_CHECK_ENABLED` | 按需 | doc_update_check |
| 应急演练合规审查 | `DRILL_AUDIT_ENABLED` | 按需 | drill_compliance_audit |
| 讲解视频 | `VIDEO_AGENT_ENABLED` | 按需 | build_agent_video_tools() 8 个 |
| 飞书消息 | `LARK_ENABLED` | 按需 | fetch_group_messages / fetch_p2p_messages / list_joined_chats / list_group_members |

「按需」的组在 `_rebuild` 里进 `builtin_deferrable`，随平台工具一起交给
`build_deferred_setup`，并以 `extra_entries={工具名: 标题+描述}` 进入 `tool_search`
的第二路索引（它们没有 capability id，不能走能力检索）。

**同步点是 `service/agent/agent_service.py::_rebuild`**（唯一的工具装配处）——加减内置工具
必须同步改 `_BUILTIN_GROUPS`。**禁止扫描 `tools/*.py` 的 `@tool`**：`tools/mock_ops_tools.py`
里有 5 个从未注入 Agent 的工具，扫出来的不是真实能力。

开关关闭或模块导入失败时**整组跳过**（只记 warning），页面不能因某个可选模块炸掉。

## 注册表初始化

`build_groups()` 只在 `registered_platforms()` 为空时调一次 `register_enabled_platforms()`：
`register_platform()` 会 `reset_index()` 清空检索向量缓存，每次请求都调会让下一次
`tool_search` 重建索引。

## API

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/api/v1/tools` | `{ok, data: {groups: [...]}}`，按分组返回全部工具 |

权限 `@require_permission('ai:agent')`。不按用户过滤（技能已不在此列）、不写库、无 Mongo 变更。

## 验证

```bash
# 目录层自检（纯 assert、不连库、不调外部接口）
python -m service.query.tests.test_tool_catalog

# 静态检查
python -m pyflakes service/query/tool_catalog.py api/v1/tools.py service/query/base/manifest.py \
  2>&1 | grep "undefined name"

cd web && npm run lint && npm run build
```

接口抽查（起后端后，带 `ai:agent` token）：`GET /api/v1/tools` → 分组包含
综合管理平台 / 动力环境数据监控 / 基础能力 / 数据分析 …，**没有「技能」组**；
web 组每项 `visibility='mode_gated'`；关掉 `VIDEO_AGENT_ENABLED` 重启后「讲解视频」组消失。

页面抽查：侧边栏「工作台 → 智能体」→ Network 只出现一次 `GET /api/v1/tools`（进页面预取）；
工具栏点「工具」→ 小窗秒开、按平台分组列出卡片，✕ 在小窗右上角；搜索 `analysis` / 分类名能筛出
命中项、空分组隐藏；关闭再打开、切页回来再打开都不再请求；
`/agent-tools` 与 `/agent` 都跳转到 `/agent-skills`。

**部署**：只改前端静态资源（后端接口未变）；无 Mongo 变更。
