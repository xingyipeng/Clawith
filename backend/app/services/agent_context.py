"""Build rich system prompt context for agents.

Architecture (inspired by Claude Code's getSystemPrompt):
- PromptContext: single dataclass holding all pre-fetched context
- Mode branches: each execution_mode has its own build function (not section add/remove)
- Explicit arrays: each build function lists sections in visible order
- Section functions: pure functions returning str | None
"""

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings

settings = get_settings()

PERSISTENT_DATA = Path(settings.AGENT_DATA_DIR)


def _agent_workspace(agent_id: uuid.UUID) -> Path:
    return PERSISTENT_DATA / str(agent_id)


def _read_file_safe(path: Path, max_chars: int = 3000) -> str:
    """Read a file, return empty string if missing. Truncate if too long."""
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        if len(content) > max_chars:
            content = content[:max_chars] + "\n...(truncated)"
        return content
    except Exception:
        return ""


def _strip_heading(text: str) -> str:
    """Strip leading markdown heading (# Title) from content."""
    if text.startswith("# "):
        return "\n".join(text.split("\n")[1:]).strip()
    return text


# ─── Skill Index ─────────────────────────────────────────────────────────────

def _parse_skill_frontmatter(content: str, filename: str) -> tuple[str, str]:
    """Parse YAML frontmatter from a skill .md file. Returns (name, description)."""
    name = filename.replace("_", " ").replace("-", " ")
    description = ""

    stripped = content.strip()
    if stripped.startswith("---"):
        end = stripped.find("---", 3)
        if end != -1:
            frontmatter = stripped[3:end].strip()
            for line in frontmatter.split("\n"):
                line = line.strip()
                if line.lower().startswith("name:"):
                    val = line[5:].strip().strip('"').strip("'")
                    if val:
                        name = val
                elif line.lower().startswith("description:"):
                    val = line[12:].strip().strip('"').strip("'")
                    if val:
                        description = val[:200]
            if description:
                return name, description

    for line in stripped.split("\n"):
        line = line.strip()
        if line in ("---",) or line.startswith("name:") or line.startswith("description:"):
            continue
        if line and not line.startswith("#"):
            description = line[:200]
            break
    if not description:
        lines = stripped.split("\n")
        if lines:
            description = lines[0].strip().lstrip("# ")[:200]

    return name, description


def _load_skills_index(agent_id: uuid.UUID) -> str:
    """Load skill index (progressive disclosure: name + description only)."""
    ws_root = _agent_workspace(agent_id)
    skills: list[tuple[str, str, str]] = []
    skills_dir = ws_root / "skills"
    if skills_dir.exists():
        for entry in sorted(skills_dir.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                skill_md = entry / "SKILL.md"
                if not skill_md.exists():
                    skill_md = entry / "skill.md"
                if skill_md.exists():
                    try:
                        content = skill_md.read_text(encoding="utf-8", errors="replace").strip()
                        name, desc = _parse_skill_frontmatter(content, entry.name)
                        skills.append((name, desc, f"{entry.name}/SKILL.md"))
                    except Exception:
                        skills.append((entry.name, "", f"{entry.name}/SKILL.md"))
            elif entry.suffix == ".md" and entry.is_file():
                try:
                    content = entry.read_text(encoding="utf-8", errors="replace").strip()
                    name, desc = _parse_skill_frontmatter(content, entry.stem)
                    skills.append((name, desc, entry.name))
                except Exception:
                    skills.append((entry.stem, "", entry.name))

    seen: set[str] = set()
    unique: list[tuple[str, str, str]] = []
    for s in skills:
        if s[0] not in seen:
            seen.add(s[0])
            unique.append(s)

    if not unique:
        return ""

    lines = [
        "以下是你可用的技能。每个技能定义了特定任务领域的详细指令。",
        "",
        "| 技能 | 说明 | 文件 |",
        "|------|------|------|",
    ]
    for name, desc, rel_path in unique:
        lines.append(f"| {name} | {desc} | skills/{rel_path} |")
    lines.append("")
    lines.append("当用户请求匹配某个技能时，先用 `read_file` 加载该技能文件的完整指令。不要猜测技能内容。")

    return "\n".join(lines)


# ─── PromptContext ─────────────────────────────────────────────────────────────

@dataclass
class PromptContext:
    """All pre-fetched context needed to build any prompt mode."""
    agent_id: uuid.UUID
    agent_name: str
    role_description: str
    current_user_name: str | None
    execution_mode: str  # "chat"|"task"|"trigger"|"heartbeat"|"a2a"|"coordinator"
    workspace_root: Path
    configured_channels: set[str] = field(default_factory=set)
    tenant_id: uuid.UUID | None = None
    timezone_name: str = "UTC"
    local_now_str: str = ""
    # File contents (pre-loaded)
    soul: str = ""
    memory: str = ""
    instructions: str = ""
    skills_text: str = ""
    relationships: str = ""
    focus: str = ""
    company_intro: str = ""


async def _build_prompt_context(
    agent_id: uuid.UUID,
    agent_name: str,
    role_description: str,
    current_user_name: str | None,
    execution_mode: str,
) -> PromptContext:
    """One-shot context builder: single DB session for all queries, then file reads."""
    from sqlalchemy import select
    from app.database import async_session

    ws_root = _agent_workspace(agent_id)

    # ── DB queries (single session) ──
    configured_channels: set[str] = set()
    tenant_id = None
    company_intro = ""

    try:
        from app.models.channel_config import ChannelConfig
        from app.models.agent import Agent as AgentModel
        from app.models.system_settings import SystemSetting

        async with async_session() as db:
            # 1. All configured channels
            ch_r = await db.execute(
                select(ChannelConfig.channel_type).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.is_configured == True,
                )
            )
            configured_channels = {row[0] for row in ch_r.all()}

            # 2. Tenant ID
            ag_r = await db.execute(select(AgentModel.tenant_id).where(AgentModel.id == agent_id))
            tenant_id = ag_r.scalar_one_or_none()

            # 3. Company intro (priority: tenant_settings > tenant-scoped system_settings > global)
            if tenant_id:
                try:
                    from app.models.tenant_setting import TenantSetting
                    ts_r = await db.execute(
                        select(TenantSetting).where(
                            TenantSetting.tenant_id == tenant_id,
                            TenantSetting.key == "company_intro",
                        )
                    )
                    ts = ts_r.scalar_one_or_none()
                    if ts and ts.value and ts.value.get("content"):
                        company_intro = ts.value["content"].strip()
                except Exception:
                    pass

            if not company_intro and tenant_id:
                r = await db.execute(
                    select(SystemSetting).where(SystemSetting.key == f"company_intro_{tenant_id}")
                )
                s = r.scalar_one_or_none()
                if s and s.value and s.value.get("content"):
                    company_intro = s.value["content"].strip()

            if not company_intro:
                r = await db.execute(
                    select(SystemSetting).where(SystemSetting.key == "company_intro")
                )
                s = r.scalar_one_or_none()
                if s and s.value and s.value.get("content"):
                    company_intro = s.value["content"].strip()
    except Exception:
        pass

    # ── Timezone ──
    from app.services.timezone_utils import get_agent_timezone, now_in_timezone
    tz_name = await get_agent_timezone(agent_id)
    local_now = now_in_timezone(tz_name)
    now_str = local_now.strftime(f"%Y-%m-%d %H:%M:%S ({tz_name})")

    # ── File reads ──
    soul = _strip_heading(_read_file_safe(ws_root / "soul.md", 2000))
    instructions = _strip_heading(_read_file_safe(ws_root / "instructions.md", 4000))
    memory = _strip_heading(
        _read_file_safe(ws_root / "memory" / "memory.md", 4000)
        or _read_file_safe(ws_root / "memory.md", 4000)
    )
    skills_text = _load_skills_index(agent_id)
    relationships = _strip_heading(_read_file_safe(ws_root / "relationships.md", 2000))
    focus = _strip_heading(
        _read_file_safe(ws_root / "focus.md", 3000)
        or _read_file_safe(ws_root / "agenda.md", 3000)
    )

    return PromptContext(
        agent_id=agent_id,
        agent_name=agent_name,
        role_description=role_description,
        current_user_name=current_user_name,
        execution_mode=execution_mode,
        workspace_root=ws_root,
        configured_channels=configured_channels,
        tenant_id=tenant_id,
        timezone_name=tz_name,
        local_now_str=now_str,
        soul=soul,
        instructions=instructions,
        memory=memory,
        skills_text=skills_text,
        relationships=relationships,
        focus=focus,
        company_intro=company_intro,
    )


# ─── Section Functions (pure, return str | None) ──────────────────────────────

def _section_identity(ctx: PromptContext) -> str:
    """身份 + 角色 + 人格——你是谁。"""
    _soul_placeholders = ("_描述你的角色和职责。_", "_Describe your role and responsibilities._")

    parts = [f"你是 {ctx.agent_name}，一名企业数字员工。"]
    if ctx.role_description:
        parts.append(f"\n## 角色\n{ctx.role_description}")
    if ctx.soul and ctx.soul not in _soul_placeholders:
        parts.append(f"\n## 人格\n{ctx.soul}")
    return "\n".join(parts)


def _section_instructions(ctx: PromptContext) -> str | None:
    """工作指令——你应该如何工作。从 instructions.md 加载。"""
    _placeholders = ("在这里定义该数字员工的工作规范。可在 Web 页面随时编辑。",)
    if ctx.instructions and not any(p in ctx.instructions for p in _placeholders):
        return f"\n## 工作指令\n{ctx.instructions}"
    return None


def _section_doing_tasks() -> str:
    return """
## 执行原则

- 先理解再行动：接到任务先用工具了解现状（read_file、list_files），不要基于假设直接执行。
- 复杂任务先拆解：超过 3 步的任务，先列出步骤再逐步执行。
- 失败先诊断：操作失败时分析原因（读错误信息、检查前提），不要盲目重试，也不要一次失败就放弃。
- 不过度设计——做被要求的事，不做没被要求的事。
- 完成后验证：执行后用 read_file 验证结果再汇报。未经验证不要说"已完成"。

### 何时主动澄清
- 指令有多种理解——列出选项并询问
- 用户的前提可能有误——指出来
- 任务范围不明确——确认边界后再执行
- 调查后仍然卡住——带着已尝试的方案上报

### 何时不要问
- 读文件就能搞清楚的——直接做
- 已经问过、对方还没回——不要重复问
- 不影响结果的细节——自己判断"""


def _section_ground_rules() -> str:
    return """
## 基本准则

如实汇报结果：
- 没有工具返回 = 没有执行。绝不在没有工具结果的情况下声称已完成。
- 工具已返回 = 已发生。直接陈述，不要对已确认的结果含糊其辞。
- 绝不编造文件内容、URL、ID 或 token。
- 绝不凭记忆描述文件内容——调用 `read_file` 获取当前数据。
- 绝不假设文件存在——先用 `list_files` 验证。
- 已通过工具创建或读取的文件，不要在回复中重复其内容——用户可以直接在工具结果中查看。只需告知路径和一句话摘要。

安全操作：
- 优先可逆操作：先读再写，先确认再删除
- 破坏性操作（delete_file、覆盖文档）前，先确认意图
- 范围不确定时，从小处着手

用户用什么语言，你就用什么语言回复。"""


def _section_task_decomposition() -> str:
    return """
## 任务拆解

### 何时拆解
- 任务需要 3 步以上或涉及多个工具/人 → 拆解为 focus items
- 用户给了一个清单 → 每项变成一个 focus item

### 何时不拆解
- 简单任务（发一条消息、读一个文件、回答一个问题）→ 直接做
- 3 步以内的小任务 → 直接做

### 示例 — "帮我调研竞品 A 和 B，写一份对比报告发给张三"
```
- [ ] research_a: 用 jina_search 调研竞品 A 的产品定位和核心功能
- [ ] research_b: 用 jina_search 调研竞品 B 的产品定位和核心功能
- [ ] write_report: 在 workspace/ 写对比报告（依赖 research_a + research_b）
- [ ] send_report: 用 send_channel_file 发给张三（依赖 write_report）
```"""


def _section_communication_style() -> str:
    return """
## 沟通风格

- 回复长度匹配复杂度——确认类简短，分析类详细
- 先说关键结论，再展开细节
- 避免元评论（"让我想想……"、"接下来我将……"）
- 多步骤任务：先说计划，再执行"""


def _section_tools(ctx: PromptContext) -> str:
    """工具选择策略。详细用法在各工具自身的 description 中。"""
    parts = ["""
## 工具使用

- 绝不说你无法访问互联网——你有搜索工具，直接用。
- 发送文件用 `send_channel_file`，不要用 `send_channel_message`。
- 代他人发消息时，必须注明来源："Hi B，A 让我告诉你……"
- 发完消息后，创建 `on_message` trigger 以便收到回复时自动唤醒。"""]

    if "feishu" in ctx.configured_channels:
        parts.append("""
### 飞书
- 发消息/日历操作前，必须先调 `feishu_user_search` 查人。
- 绝不使用 `{document_token}` 占位符——只用工具返回的真实链接。
- 不要因为之前工具报错就跳过调用——权限可能已修复。""")

    if "atlassian" in ctx.configured_channels:
        parts.append("""
### Atlassian
- 绝不编造 issue ID、page URL 或 component name。""")

    return "\n".join(parts)


def _section_company_info(ctx: PromptContext) -> str | None:
    if ctx.company_intro:
        return f"\n## 公司信息\n{ctx.company_intro}"
    return None


def _section_workspace() -> str:
    return """
## 工作区

你的专属工作区文件结构：
- `focus.md` — 当前跟踪事项（醒来时必须先读）
- `task_history.md` — 已完成任务归档
- `instructions.md` — 你的工作指令和标准
- `soul.md` — 你的人格定义
- `memory/memory.md` — 长期记忆与笔记
- `memory/reflections.md` — 自主思考日记
- `skills/` — 技能定义文件
- `workspace/` — 工作文件，按项目组织
- `relationships.md` — 人际关系列表
- `enterprise_info/` — 共享企业信息"""


def _section_focus_format() -> str:
    return """
## 工作项

在 `focus.md` 中用以下清单格式跟踪工作：
```
- [ ] identifier_name: 跟踪的事项
- [/] in_progress_item: 正在进行
- [x] done_item: 已完成
```

纪律：
- 同一时间只有一个 [/]（进行中）——完成当前项再开始下一项
- 完成后立即标记 [x]，不要攒批
- 绝不在以下情况标记 [x]：操作失败、仅部分完成、未验证结果
- 被阻塞时，记录阻塞原因，转去处理未阻塞的项

醒来时先读 focus items。它们是参考，不是命令——根据时机、上下文和紧急程度判断行动。积压的已完成项归档到 `task_history.md`。"""


def _section_triggers_guide() -> str:
    return """
## 触发器

用触发器工具管理你的唤醒条件：
- `set_trigger` — 类型：`cron`、`once`、`interval`、`poll`、`on_message`、`webhook`
- `update_trigger` / `cancel_trigger` / `list_triggers`

**工作项-触发器绑定：** 创建任务触发器前，先添加一个 focus item。设置 `focus_ref` 关联。focus item 完成（`[x]`）后取消对应触发器。系统触发器（如 heartbeat）不受此限。

**写 `reason` —— 未来的你唯一的上下文：**
触发器触发时，你醒来后对当前对话没有任何记忆。`reason` 必须包含：
- 目标：做什么、谁请求的、目标对象是谁
- 操作步骤：具体要做什么
- 边界情况："等 X 分钟"、已完成、没回复、意外回复怎么办
- 后续：接下来创建/取消什么触发器

好的 reason："每 1 分钟给覃睿发飞书消息催电影票（Ray 让催的）。如果回复'等 X 分钟' → 取消 interval，设 once trigger，重建 on_message。如果说搞定了 → 取消所有 trigger，通知 Ray，标记 focus [x]。"
差的 reason："提醒覃睿" """


def _section_skills_index(ctx: PromptContext) -> str | None:
    if ctx.skills_text:
        return f"\n## 技能\n{ctx.skills_text}"
    return None


def _section_relationships(ctx: PromptContext) -> str | None:
    if ctx.relationships and "暂无" not in ctx.relationships and "None yet" not in ctx.relationships:
        return f"\n## 人际关系\n{ctx.relationships}"
    return None


# ── Mode-specific preambles ──

def _section_task_preamble() -> str:
    return """
## 任务执行模式

你正在自主执行一个任务（不是对话）。
- 将任务拆解为步骤，逐步执行
- 积极使用工具收集信息、发消息、读写文件
- 最后提供详细的执行报告
- 不要问后续问题——主动推进"""


def _section_trigger_preamble() -> str:
    return """
## 触发器唤醒

你被一个触发器唤醒。你对之前的对话没有任何记忆。
你的上下文在下方的用户消息中——包含触发器的 reason 和匹配数据。
执行 reason 中的指令。更新 focus.md 并按需调整触发器。"""


def _section_a2a_rules() -> str:
    return """
## 数字员工间协议

你正在回复另一位数字员工的消息。
- 简洁、有帮助地回复，聚焦交付物
- 写完对方需要的文件后，调用 `send_file_to_agent` 交付——对方无法访问你的 workspace
- 不要只告诉文件路径——必须主动交付"""


def _section_coordinator_role() -> str:
    return """
## 你的角色：协调者

你正在协调一个需要多位数字员工参与的任务。

### 阶段

| 阶段 | 谁来做 | 目的 |
|------|--------|------|
| 调研 | 其他 agent（可并行） | 收集信息、调查背景 |
| 综合 | **你自己** | 阅读调研结果，理解全貌，制定具体执行方案 |
| 执行 | 其他 agent | 按你的方案执行具体操作 |
| 验证 | 你自己或其他 agent | 检查执行结果是否符合要求 |

### 委托规则
- **永远先综合再委托**：当 agent 返回调研结果后，你必须先理解结果，然后写出具体的执行方案。
  绝不写"根据你的调研结果去执行"——这是在转移理解责任。
  正确做法："在 workspace/ 写一份对比报告，包含以下要点：1) A 的定价是 XX 2) B 的优势是 YY..."
- **每个委托必须自包含**：对方看不到你和用户的对话。委托消息必须包含：
  - 完整的任务描述（做什么、为什么）
  - 必要的上下文（文件路径、数据、背景）
  - 完成标准（什么算做完了）
- **并发规则**：只读任务（调研、查询）可以并行派出多个 agent；写入任务（发消息、创建文档）一次一个
- **等待结果时不要预测**：agent 还没回复就不要告诉用户"应该快好了"或预测结果

### 跟踪
- 每个委托对应一个 focus item + on_message trigger
- Agent 回复后更新 focus item 状态
- 所有子任务完成后，综合结果汇报给用户"""


# ── Dynamic section helpers ──

def _section_memory(ctx: PromptContext) -> str | None:
    placeholders = (
        "_这里记录重要的信息和学到的知识。_",
        "_Record important information and knowledge here._",
    )
    if ctx.memory and ctx.memory not in placeholders:
        return f"\n## 记忆\n{ctx.memory}"
    return None


def _section_focus_data(ctx: PromptContext) -> str | None:
    if ctx.focus and ctx.focus.strip() not in ("# Focus", "# Agenda", "（暂无）", ""):
        return f"\n## 当前工作\n{ctx.focus}"
    return None


async def _section_active_triggers(ctx: PromptContext) -> str | None:
    try:
        from app.database import async_session
        from app.models.trigger import AgentTrigger
        from sqlalchemy import select as sa_select
        async with async_session() as db:
            result = await db.execute(
                sa_select(AgentTrigger).where(
                    AgentTrigger.agent_id == ctx.agent_id,
                    AgentTrigger.is_enabled == True,
                )
            )
            triggers = result.scalars().all()
            if triggers:
                lines = ["你当前有以下活跃的触发器："]
                for t in triggers:
                    config_str = str(t.config)[:80]
                    reason_str = (t.reason or "")[:500]
                    ref_str = f" (focus: {t.focus_ref})" if t.focus_ref else ""
                    lines.append(f"\n- **{t.name}** [{t.type}]{ref_str}\n  配置: `{config_str}`\n  Reason: {reason_str}")
                return "\n## 活跃触发器\n" + "\n".join(lines)
    except Exception:
        pass
    return None


def _section_time_info(ctx: PromptContext) -> str:
    return f"\n## 当前时间\n{ctx.local_now_str}\n你的时区是 **{ctx.timezone_name}**。设置 cron 触发器时使用此时区。"


def _section_current_user(ctx: PromptContext) -> str | None:
    if ctx.current_user_name:
        return f"\n## 当前对话\n你正在和 **{ctx.current_user_name}** 聊天。在合适的时候称呼对方的名字。"
    return None


# ─── Mode Build Functions (explicit arrays, visible order) ─────────────────────

async def _build_standard_prompt(ctx: PromptContext) -> tuple[str, str]:
    """Chat / Task mode — full prompt with all capabilities."""
    is_task = ctx.execution_mode == "task"

    static_sections = [
        _section_identity(ctx),
        _section_instructions(ctx),
        _section_doing_tasks(),
        _section_ground_rules(),
        _section_task_decomposition() if is_task else None,
        _section_communication_style(),
        _section_tools(ctx),
        _section_company_info(ctx),
        _section_workspace(),
        _section_focus_format(),
        _section_triggers_guide(),
        _section_skills_index(ctx),
        _section_relationships(ctx),
        _section_task_preamble() if is_task else None,
    ]

    dynamic_sections = [
        _section_memory(ctx),
        _section_focus_data(ctx),
        await _section_active_triggers(ctx),
        _section_time_info(ctx),
        _section_current_user(ctx),
    ]

    static = "\n".join(s for s in static_sections if s)
    dynamic = "\n".join(s for s in dynamic_sections if s)
    return static, dynamic


async def _build_heartbeat_prompt(ctx: PromptContext) -> tuple[str, str]:
    """Heartbeat — slim prompt: self-awareness + tools."""
    static = "\n".join(filter(None, [
        _section_identity(ctx),
        _section_ground_rules(),
        _section_company_info(ctx),
        _section_tools(ctx),
        _section_skills_index(ctx),
        _section_relationships(ctx),
    ]))
    dynamic = "\n".join(filter(None, [
        _section_memory(ctx),
        _section_time_info(ctx),
    ]))
    return static, dynamic


async def _build_coordinator_prompt(ctx: PromptContext) -> tuple[str, str]:
    """Coordinator — multi-agent orchestration."""
    static = "\n".join(filter(None, [
        _section_identity(ctx),
        _section_instructions(ctx),
        _section_coordinator_role(),
        _section_ground_rules(),
        _section_doing_tasks(),
        _section_task_decomposition(),
        _section_tools(ctx),
        _section_company_info(ctx),
        _section_workspace(),
        _section_focus_format(),
        _section_triggers_guide(),
        _section_skills_index(ctx),
        _section_relationships(ctx),
    ]))
    dynamic = "\n".join(filter(None, [
        _section_memory(ctx),
        _section_focus_data(ctx),
        await _section_active_triggers(ctx),
        _section_time_info(ctx),
        _section_current_user(ctx),
    ]))
    return static, dynamic


async def _build_a2a_prompt(ctx: PromptContext) -> tuple[str, str]:
    """Agent-to-Agent — slim + A2A protocol."""
    static = "\n".join(filter(None, [
        _section_identity(ctx),
        _section_ground_rules(),
        _section_communication_style(),
        _section_a2a_rules(),
        _section_company_info(ctx),
        _section_relationships(ctx),
    ]))
    dynamic = "\n".join(filter(None, [
        _section_memory(ctx),
        _section_time_info(ctx),
        _section_current_user(ctx),
    ]))
    return static, dynamic


async def _build_trigger_prompt(ctx: PromptContext) -> tuple[str, str]:
    """Trigger — wake-up context + execution capabilities."""
    static = "\n".join(filter(None, [
        _section_identity(ctx),
        _section_instructions(ctx),
        _section_trigger_preamble(),
        _section_doing_tasks(),
        _section_ground_rules(),
        _section_company_info(ctx),
        _section_workspace(),
        _section_focus_format(),
        _section_triggers_guide(),
        _section_tools(ctx),
        _section_skills_index(ctx),
        _section_relationships(ctx),
    ]))
    dynamic = "\n".join(filter(None, [
        _section_memory(ctx),
        _section_focus_data(ctx),
        await _section_active_triggers(ctx),
        _section_time_info(ctx),
    ]))
    return static, dynamic


# ─── Public API (backward-compatible signature) ───────────────────────────────

async def build_agent_context(
    agent_id: uuid.UUID,
    agent_name: str,
    role_description: str = "",
    current_user_name: str = None,
    execution_mode: str = "chat",
) -> tuple[str, str]:
    """Build a rich system prompt incorporating agent's full context.

    Args:
        execution_mode: "chat" | "task" | "trigger" | "heartbeat" | "a2a" | "coordinator"

    Returns:
        (static_prompt, dynamic_prompt) — static is cacheable, dynamic changes per turn.
    """
    ctx = await _build_prompt_context(agent_id, agent_name, role_description, current_user_name, execution_mode)

    if execution_mode == "heartbeat":
        return await _build_heartbeat_prompt(ctx)
    if execution_mode == "coordinator":
        return await _build_coordinator_prompt(ctx)
    if execution_mode == "a2a":
        return await _build_a2a_prompt(ctx)
    if execution_mode == "trigger":
        return await _build_trigger_prompt(ctx)

    # Default: chat / task
    return await _build_standard_prompt(ctx)
