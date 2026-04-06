# Claude Code 最佳实践工具库

本目录用于沉淀 Claude Code 的最佳实践、配置和工作流。

## 项目概述

这是一个 Claude Code 配置和工作流的最佳实践仓库，参考 [shanraisshan/claude-code-best-practice](https://github.com/shanraisshan/claude-code-best-practice)。

## MCP 服务器

| 服务器 | 用途 |
|--------|------|
| `context7` | 获取最新库文档，防止过时API幻觉 |
| `playwright` | 浏览器自动化，UI测试验证 |

## 推荐的MCP组合

研究 (Context7/DeepWiki) → 调试 (Playwright/Chrome) → 文档 (Excalidraw)

## 目录结构

```
.claude/
├── agents/       # 自定义子代理
├── commands/     # 自定义命令
├── skills/       # 技能定义
├── hooks/        # 钩子脚本
└── rules/        # 规则文档
.mcp.json         # MCP服务器配置
```

## 可用子代理

| 代理 | 用途 |
|------|------|
| `planner` | 复杂功能的实现规划 |
| `code-reviewer` | 代码审查 |
| `tdd-guide` | TDD 测试驱动开发引导 |
| `security-reviewer` | 安全审查 |
| `build-error-resolver` | 构建错误修复 |

## 可用命令

| 命令 | 用途 |
|------|------|
| `/code-review` | 审查当前变更 |
| `/pr-summarize` | 生成 PR 总结 |
| `/architecture` | 分析项目架构 |

## 开发工作流

```
研究 & 复用 → 规划(planner) → TDD(tdd-guide) → 编码 → 审查(code-reviewer) → 安全审查 → 提交
```

## 上下文管理

- 上下文超过 50% 时执行 `/compact`
- CLAUDE.md 文件保持 200 行以内
- 大任务拆分成多个子代理

## 模型选择

| 任务 | 模型 |
|------|------|
| 代码搜索、探索 | haiku |
| 复杂编码、编排 | sonnet |
| 架构决策、深度推理 | opus |
