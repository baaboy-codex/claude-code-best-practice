# Claude Code 最佳实践工具库

本目录用于沉淀 Claude Code 的最佳实践、配置和工作流。

## 项目概述

Claude Code 配置和工作流的最佳实践仓库，参考 [shanraisshan/claude-code-best-practice](https://github.com/shanraisshan/claude-code-best-practice)。

## 目录结构

```
.claude/
├── agents/           # 自定义子代理 (5个)
├── commands/         # 自定义命令 (6个)
├── skills/           # 技能定义 (6个)
├── hooks/            # 钩子脚本
└── rules/            # 规则文档
.mcp.json             # MCP服务器配置
```

## MCP 服务器

| 服务器 | 用途 |
|--------|------|
| `context7` | 获取最新库文档，防止过时API幻觉 |
| `playwright` | 浏览器自动化，UI测试验证 |

## 可用子代理 (Agents)

| 代理 | 用途 | 模型 |
|------|------|------|
| `planner` | 复杂功能的实现规划 | sonnet |
| `code-reviewer` | 代码审查 | sonnet |
| `tdd-guide` | TDD测试驱动开发引导 | sonnet |
| `security-reviewer` | 安全审查 | sonnet |
| `build-error-resolver` | 构建错误修复 | sonnet |

## 可用命令 (Commands)

| 命令 | 用途 |
|------|------|
| `/code-review` | 审查当前变更 |
| `/pr-summarize` | 生成PR总结 |
| `/architecture` | 分析项目架构 |
| `/book-summary` | 书籍摘要工作流 |

## 可用技能 (Skills)

| 技能 | 用途 | 类型 |
|------|------|------|
| `/simplify` | 代码优化重构 | user-invocable |
| `/loop` | 定时循环任务 | user-invocable |
| `/debug` | 调试失败命令 | user-invocable |
| `/batch` | 批量文件操作 | user-invocable |
| `/context-optimization` | 上下文优化 | user-invocable |
| `/humanizer` | 去除AI写作痕迹 | user-invocable |

## 工作流架构

### Command → Agent → Skill 编排模式

```
User → Command → Agent (with preloaded skill) → Skill → Output
```

示例：`/book-summary`
1. Command: 问用户书籍路径
2. Agent: 预加载 `book-fetcher` skill 获取元数据
3. Skill: `book-summary-creator` 生成摘要

### 开发工作流

```
研究 & 复用 → 规划(planner) → TDD(tdd-guide) → 编码 → 审查(code-reviewer) → 安全审查 → 提交
```

## 上下文管理

- 上下文超过 50% 时执行 `/compact`
- CLAUDE.md 文件保持 200 行以内
- 大任务拆分成多个子代理
- 优先使用 haiku 处理简单任务

## 模型选择

| 任务 | 模型 |
|------|------|
| 代码搜索、探索 | haiku |
| 复杂编码、编排 | sonnet |
| 架构决策、深度推理 | opus |

## Hooks 钩子

| 事件 | 用途 |
|------|------|
| PreToolUse | 工具执行前检查 |
| Stop | 会话停止时记录 |
| SessionStart | 会话启动时记录 |

## 配置优先级

1. 托管配置 (MDM) — 不可覆盖
2. 命令行参数
3. `settings.local.json` (git-ignored)
4. `settings.json` (团队共享)
5. `~/.claude/settings.json` (全局)
