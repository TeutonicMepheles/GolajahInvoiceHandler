# InvoicesHandler Documentation

本目录保存跨模块架构约束、Feature 实施计划和少量长期有效的技术决策。

## 使用入口

1. 先读根目录 `AGENTS.md`，确认工程级约束和验证命令。
2. 读 [架构说明](architecture.md)，确认模块职责和允许的依赖方向。
3. 在 [Plan 索引](plans/README.md) 中定位当前 Feature 的 owning Plan。
4. 当前实现事实仍以代码、配置和测试为准；Plan 中的验证只代表记录时实际执行的范围。

## 文档边界

- `plans/`：一次 Feature 或结构迁移的目标、范围、里程碑和验证证据。
- Feature `README.md`：模块当前责任、公共入口和直接依赖。
- 架构决策只有在长期影响多个 Feature 且难以从代码直接看出时，才写入 `adr/`；不记录普通 Session 日志。
