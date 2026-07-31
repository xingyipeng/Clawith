"""Post-conversation memory extraction and consolidation.

Inspired by Claude Code's Extract Memories system:
after each conversation, an LLM pass reviews the dialogue and updates
the agent's long-term memory — extracting new info, deduplicating,
and removing contradicted facts.
"""

import logging
import uuid
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()
PERSISTENT_DATA = Path(settings.AGENT_DATA_DIR)

EXTRACT_PROMPT = """你是 {agent_name} 的记忆管理器。
审阅下方对话，更新该 agent 的长期记忆。

## 当前记忆
{existing_memory}

## 最近对话
{conversation_messages}

## 指令
输出完整的更新后记忆内容（使用与对话相同的语言）。规则：
- 提取：用户偏好、重要决策、项目上下文、经验教训
- 重点提取：当前对话的核心目的是什么（如"用户要做一个即时通讯项目"），记入项目上下文
- 重点提取：用户提到的技术选型、风格偏好、质量要求
- 跳过：寒暄、常规操作、临时任务细节（这些属于 focus.md）
- 新信息与已有记忆矛盾时，更新旧条目（不要两个都保留）
- 合并相关条目到同一章节
- 总量控制在 {max_chars} 字符以内
- 使用以下结构：

## 用户偏好
（沟通风格、输出格式偏好、关注的细节）

## 重要约定
（明确说过的规则和要求）

## 项目上下文
（正在进行的项目、技术选型、架构决策、项目目标）

## 经验教训
（出过的错、用户不满意的地方、改进方法）

如果对话中没有值得记录的内容，原样输出已有记忆。
只输出记忆内容，不要加解释或 markdown 代码围栏。"""


def _read_file_safe(path: Path, max_chars: int = 4000) -> str:
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        if len(content) > max_chars:
            content = content[:max_chars] + "\n...(truncated)"
        return content
    except Exception:
        return ""


async def extract_and_consolidate_memory(
    agent_id: uuid.UUID,
    agent_name: str,
    conversation_messages: list[dict],
    max_chars: int = 4000,
) -> None:
    """Extract important information from a conversation and update memory.

    Called asynchronously after each chat conversation ends.
    Uses a separate LLM call to review the conversation and produce
    an updated memory file.
    """
    try:
        ws = PERSISTENT_DATA / str(agent_id)
        mem_path = ws / "memory" / "memory.md"

        # Ensure directory exists
        mem_path.parent.mkdir(parents=True, exist_ok=True)

        # Read existing memory
        existing = _read_file_safe(mem_path, max_chars)
        # Strip heading
        if existing.startswith("# "):
            existing = "\n".join(existing.split("\n")[1:]).strip()

        # Build conversation text (last 20 messages, truncated)
        recent = conversation_messages[-20:]
        conv_lines = []
        for m in recent:
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, str) and content.strip():
                conv_lines.append(f"[{role}]: {content[:500]}")
        conv_text = "\n".join(conv_lines)

        if not conv_text.strip():
            return  # Nothing to extract from

        # Build extraction prompt
        prompt = EXTRACT_PROMPT.format(
            agent_name=agent_name,
            existing_memory=existing or "(empty)",
            conversation_messages=conv_text,
            max_chars=max_chars,
        )

        # Get agent's LLM model
        from sqlalchemy import select
        from app.database import async_session
        from app.models.agent import Agent
        from app.models.llm import LLMModel

        async with async_session() as db:
            ag_r = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = ag_r.scalar_one_or_none()
            if not agent or not agent.primary_model_id:
                return

            model_r = await db.execute(select(LLMModel).where(LLMModel.id == agent.primary_model_id))
            model = model_r.scalar_one_or_none()
            if not model or not model.enabled:
                return

        # Call LLM
        from app.services.llm_utils import create_llm_client, LLMMessage
        client = create_llm_client(model)

        response = await client.complete(
            messages=[
                LLMMessage(role="system", content="你是一个记忆提取助手。严格按照指令执行。"),
                LLMMessage(role="user", content=prompt),
            ],
            max_tokens=1000,
            temperature=0,
        )

        # Extract text from response
        new_memory = ""
        if isinstance(response, str):
            new_memory = response.strip()
        elif hasattr(response, "content"):
            new_memory = response.content.strip() if response.content else ""

        # Validate and write back
        if new_memory and len(new_memory) > 20:
            # Remove any markdown code fences the model might add
            if new_memory.startswith("```"):
                lines = new_memory.split("\n")
                new_memory = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

            mem_path.write_text(f"# Memory\n\n{new_memory}\n", encoding="utf-8")
            logger.info(f"[MemoryExtractor] Updated memory for agent {agent_name} ({len(new_memory)} chars)")
        else:
            logger.debug(f"[MemoryExtractor] No memory update needed for agent {agent_name}")

    except Exception as e:
        logger.warning(f"[MemoryExtractor] Failed for agent {agent_id}: {e}")
