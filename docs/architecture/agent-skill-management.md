# 智能体（Agent Skill 列表 / 详情 / 启停 / 删除）

> 2026-11-09 上线。侧边栏「工作台 → 智能体」（`/agent-skills`），权限 `ai:agent`；
> 旧地址 `/agent` 跳转到 `/agent-skills`。
> 后端 `api/v1/skills.py` + `service/query/skills/{catalog,states}.py`，前端
> `web/src/pages/AgentSkillsPage.tsx` + `web/src/components/agent-skills-view.tsx` + `web/src/api/skills.ts`。

## 页面口径

智能体页回答一个问题：**平台现在有哪些技能、哪些是我自己的、开没开**。

- **全部 / 公共技能 / 自定义技能** 三个 tab（带数量）+ 页头搜索框（标题 / 名字 / 描述）。
- 卡片：图标块（按名字 hash 取色，纯视觉）+ 标题 + 来源 Badge + 技能名（等宽）+ 两行描述 +
  「已启用 / 已停用」状态。
- **公共技能只读**：卡片上没有开关和删除按钮，只能打开详情。
- **自定义技能**额外有启用开关（乐观更新，失败回滚并提示）与删除（二次确认）。
- 详情弹窗：**概述**（描述首句）+ **使用场景**（frontmatter `when_to_use`，缺失时回退描述
  后续句）+ 分类/领域/类型 + 附带文档（`references/`）清单。**不展示 `SKILL.md` 原文**
  ——那是给模型看的内部文档；这些字段**随技能列表一次下发**，点「详情」不再请求接口。
- 页头「通过对话创建技能」跳 `/assistant` —— **改内容仍走对话里的 `skill_manage`**，
  页面只做查看与管理，不做编辑器。

## 为什么公共技能只读

启用/停用是**按用户的运行时偏好**，不是技能本身的属性。公共技能（`skills/<name>/`，
`owner=''`）被所有人共享，如果允许停用：

- 要么在共享 agent 图上做按用户过滤（图会因用户不同而无法复用，需要按用户重建）；
- 要么把开关写进技能目录（一份文件表达多个用户的偏好，语义上就不对）。

所以公共技能不提供停用入口，自定义技能（`skills/users/<uid>/`）天然按人隔离，开关才有意义。

## 启用状态存储

Mongo 集合 `skill_states`，一行 = 一个用户对一个技能：

```json
{ "user_id": "u1", "skill_name": "my_report", "enabled": false, "updated_at": "..." }
```

唯一索引 `(user_id, skill_name)` 在 `config/mongodb_conn.py` 的 `init_indexes()` 里建，
随启动自动创建。

- 无记录 = **启用**（默认开）。
- 读写异常一律 **fail-open**（按启用处理 + warning）：开关是偏好，不能因为 Mongo 抖一下
  就让技能整体不可用。
- 删除技能时顺带删掉开关记录（best-effort），防同名重建继承旧状态。

## 运行期过滤位置

开关**不重建共享图**，在两处运行时判断：

1. `core/agent/deferred.py` 的 `tool_search`：本人自定义技能若在
   `get_disabled_skill_names(current_user)` 里，不 promote、不进发现卡片 —— 模型看不到它。
2. `service/query/skills/client.py` 的 `call()`：`_check_owner` 之后若状态为停用，直接抛
   `SkillsError('技能 <name> 已停用（可在「工作台 → 智能体」重新启用）')` —— 覆盖
   「停用之前已经被 promote、会话状态里还留着」的情况。

两处都**只对自定义技能生效**（`owner` 非空），公共技能不受影响。启用状态走 Mongo，
**跨 worker 立即一致**（不像技能目录那样受单 worker 热加载限制）。

## 列表数据源（详情字段随列表下发）

`service/query/skills/catalog.py` 读**进程内快照** `discover_skills_cached()`，不走全局平台
注册表：skills 平台是在首次构建 Agent 图时才注册的（`service/query/bootstrap.py`），页面可能
在第一次对话之前就被打开，走注册表会漏技能。

快照在 `main.py` lifespan 启动时建立（日志「技能快照就绪: N 条」），写盘/删除经
`reload_skills()` 刷新；**不按请求扫盘** —— 每次请求实时遍历 `skills/` 时，任何一次目录
不可读、文件被并发写或编码异常都会 500 掉整张列表。扫描本身也做了兜底：单个技能目录/文件
失败只跳过该条（`OSError` + `UnicodeDecodeError`），`iterdir()` 失败只跳过那一层。

- `main_capabilities(caps)`：只留 `skill.<name>`，排除 references 子能力 `skill.<name>.<stem>`。
- `list_catalog(user_id, disabled)`：公共技能 + 本人技能；他人技能不出现。每项除
  `name/title/description/category/domain/kind/owner/editable/enabled` 外还带详情字段，
  **全部来自同一份快照**（不读盘、不额外 IO）：
  - `overview`：`description` 首句（按 `。！？!?` 切，无句末标点取整段）；
  - `when_to_use`：技能 frontmatter 的 `when_to_use`，为空时回退 `description` 首句之后的
    剩余文本（再没有就是空串）—— 用户自建技能没写该字段时仍能给出使用场景；
  - `references`：从快照里同 owner 的 `skill.<name>.<stem>` 子能力聚合
    `{type: stem, name, description}`。
- **不再有 `skill_detail`**：详情字段已并入列表，按请求定位目录/读盘那条路已删掉
  （Agent 运行时仍由 `client.build_caller()` 按需读 `SKILL.md` 全文，互不影响）。

## 删除后的热加载链路

`DELETE /api/v1/skills/{name}` 只对本人技能生效：

```
storage.delete_skill(user_id, name)   # 删目录
  → remove_skill_state(user_id, name) # 清开关记录
  → reload_skills()                   # refresh_skills_cache() + register_platform(...) + bump_skills_version()
```

`service/query/skills/platform.py::reload_skills()` 是**唯一热加载入口**，对话内的
`skill_manage`（`tools/skill_tools.py::_reload_skills`）也调它，两条路径行为一致。

**多 worker 边界**：`reload_skills()` 只在本 worker 生效（与既有 `skill_manage` 同源、
同一个限制），下一条消息触发本 worker 的 agent 重建；其它 worker 的技能列表**重启后**
跟上。启停因为是运行时 Mongo 判断，不受这个限制。

## API

统一 `@require_permission('ai:agent')`，人从 `request.state.user_id` 取。

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/api/v1/skills` | `{ok, data: {skills: [...]}}`，公开 + 本人，**含详情字段** |
| PATCH | `/api/v1/skills/{name}/enabled` | body `{enabled: bool}`；仅本人技能，否则 404 |
| DELETE | `/api/v1/skills/{name}` | 仅本人技能；删目录 → 清开关 → 热加载 |

PATCH 不触发热加载（开关是运行时偏好，不需要重建图）；DELETE 需要（技能要立刻从
`tool_search` 里消失）。

## 验证

```bash
# 目录层自检（纯 assert、不连库、不写真实技能目录）
python -m service.query.skills.tests.test_catalog

# 静态检查
python -m pyflakes api/v1/skills.py service/query/skills/{catalog,states,client,platform,discover}.py \
  service/query/tool_catalog.py core/agent/deferred.py tools/skill_tools.py \
  2>&1 | grep "undefined name"

cd web && npm run lint && npm run build
```

端到端（部署后）：启动日志出现「技能快照就绪: N 条」→ 登录有 `ai:agent` 的账号 →
侧边栏「工作台 → 智能体」→ 官方技能只读、详情可打开（**网络面板无新请求**，弹窗显示
概述 / 使用场景 / 分类领域类型 / 附带文档，没有 `SKILL.md` 原文）→ 自定义技能开关停用 →
回到对话里让它用该技能（`tool_search` 不再返回、硬调报「已停用」）→ 重新启用后恢复 →
删除后列表消失（本 worker 立即生效，其它 worker 重启后一致）、对话里 `skill_manage list`
也不再出现。`GET /api/v1/skills/{name}` 已下线（404）。

**部署**：重建 `rz-backend` 容器 + 前端静态资源；Mongo 索引随启动 `init_indexes()` 自动建。
