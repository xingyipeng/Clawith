---
name: 跟进任务管理
description: 当需要追踪某件事、提醒某人、等待回复、定期检查状态时使用
---

# 跟进任务管理

## 触发场景
"帮我盯着 X"、"每隔 N 分钟提醒 Y"、"等 Z 回复了告诉我"、"跟进一下这件事"

---

## 核心原则
**每一个跟进任务 = focus.md 条目 + trigger**，两者必须同时创建，缺一不可。

---

## 强制工作流

### Step 1 — 在 focus.md 创建追踪条目
```
write_file(path="focus.md", content="
- [ ] <task_identifier>: <清晰描述这件事是什么、谁发起的、目标是什么>
")
```
identifier 规则：简短 snake_case，如 `follow_qinrui_ticket`

### Step 2 — 创建 Trigger（必须含完整 reason）
根据场景选择 trigger 类型：

**场景 A：定时提醒某人**
```
set_trigger(
  name="nag_<人名>_<任务>",
  type="interval",
  config={"minutes": <N>},
  focus_ref="<Step1的identifier>",
  reason="每隔N分钟向<人名>发送飞书消息提醒<做什么事>（由<谁>发起请求）。
          发送后保持此 interval trigger 活跃。
          若对方说'等X分钟'→取消此interval，设置X分钟后的once trigger恢复，并重建on_message trigger。
          若对方说完成→取消所有相关trigger，通知发起人，将focus条目标记为[x]。"
)
```

**场景 B：等待某人回复**
```
set_trigger(
  name="wait_<人名>_reply",
  type="on_message",
  config={"from_user_name": "<人名>"},
  focus_ref="<Step1的identifier>",
  reason="<人名>回复了关于<事项>的消息时触发。
          处理逻辑：
          1) 若回复表示完成→取消相关trigger，通知发起人，更新focus为[x]
          2) 若回复'等X分钟'→取消interval，X分钟后重启，重建on_message
          3) 其他回复→评估意图，继续跟进"
)
```

**场景 C：定时检查状态**
```
set_trigger(
  name="check_<任务>",
  type="cron",
  config={"cron": "0 9 * * 1-5"},  # 工作日早9点
  focus_ref="<Step1的identifier>",
  reason="每工作日早9点检查<任务>的状态。检查方式：read_file workspace/<相关文件>。
          若状态变化→更新focus条目，通知相关人员。
          若任务完成→取消此trigger，标记focus为[x]。"
)
```

### Step 3 — 告知用户
汇报：
- 已创建什么 trigger（类型、频率）
- focus.md 中的追踪条目
- 触发条件是什么

---

## ❌ 禁止事项
- 不得创建 trigger 而不创建对应的 focus 条目
- reason 字段不得写"提醒某人"这种模糊内容——必须包含完整的处理逻辑
- 不得在 set_trigger 返回结果前告知用户"已设置提醒"
