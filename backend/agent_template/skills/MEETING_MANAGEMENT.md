---
name: 会议管理
description: 创建、修改、查询日历会议，以及整理会议记录时使用
---

# 会议管理

## 触发场景
用户说"帮我约个会"、"创建一个会议"、"安排 X 和 Y 见面"、"整理会议纪要"时启用。

---

## 强制工作流

### 创建会议

**Step 1 — 收集信息（缺一不可）**
在调用工具前，确认以下信息（已知的不用再问）：
- [ ] 会议主题
- [ ] 开始时间和结束时间（含时区，默认 Asia/Shanghai）
- [ ] 参会人姓名（不需要 email，系统自动查找）

**Step 2 — 查询空档（可选但推荐）**
```
feishu_calendar_list(start_time="<今日起>", end_time="<+3天>")
```
确认参会人无冲突后再创建。

**Step 3 — 创建会议**
```
feishu_calendar_create(
  summary="<主题>",
  start_time="<ISO-8601 +08:00>",
  end_time="<ISO-8601 +08:00>",
  attendee_names=["<姓名1>", "<姓名2>"]
)
```
⚠️ 必须等工具返回 event_id 后才能告知用户"会议已创建"。

**Step 4 — 通知参会人（如需要）**
使用工具返回的真实会议链接发送通知：
```
send_feishu_message(member_name="<姓名>", content="会议已创建：<真实链接>")
```

---

### 整理会议记录

**Step 1 — 创建飞书文档**
```
feishu_doc_create(title="会议记录_<主题>_<日期>")
```

**Step 2 — 写入内容**
```
feishu_doc_append(document_token="<工具返回的真实token>", content="
# 会议记录

**时间**：
**参会人**：
**主持人**：

## 议题与讨论

## 决策事项

## 待办事项
| 负责人 | 事项 | 截止日期 |
|--------|------|---------|

## 下次会议
")
```

**Step 3 — 分享文档**
把工具返回的真实文档链接发给参会人。

---

## ❌ 禁止事项
- 不得在 feishu_calendar_create 返回结果前说"会议已创建"
- 不得自行构造日历链接（如 calendar.feishu.cn/xxx）
- 不得询问用户 email 或 open_id——直接用姓名，系统自动查找
