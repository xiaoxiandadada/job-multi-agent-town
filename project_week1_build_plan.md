# AI 求职执行 Agent 项目：第 1 周构建计划

## 项目目标

构建一个能每天辅助求职的 Agent 系统：

- 生成每日学习任务。
- 搜索并追踪岗位。
- 分析 JD 与简历匹配度。
- 提取缺口技能。
- 推荐当天最重要的学习任务。

## 第 1 周只做最小可行版本

### Day 1：定义数据结构

输出：

- `Job` 字段：公司、岗位、方向、链接、匹配度、缺口技能、简历版本。
- `DailyPlan` 字段：日期、SQL 题、LeetCode 题、ML 题、Agent 学习、岗位动作。

### Day 2：岗位追踪模块

输出：

- 使用现有 `ai-job-tracker` skill 更新 `job_tracker.md`。
- 写出匹配度规则。

### Day 3：JD 分析模块

输出：

- 输入一段 JD。
- 输出岗位方向、关键词、缺口技能、推荐简历版本。

### Day 4：日报生成模块

输出：

- 从学习材料中选择当天任务。
- 写入 `daily/YYYY-MM-DD.md`。

### Day 5：评估模块

输出：

- 检查日报是否具体。
- 检查岗位是否去重。
- 检查缺口技能是否具体。

## 技术栈建议

- Python
- Markdown 文件存储
- JSON 作为中间结构
- 后续可加 LangGraph 管理 workflow

## README 项目包装方向

这个项目可以包装成：

```text
AI Job Search Agent: a daily autonomous workflow for job discovery, JD-resume matching, skill-gap extraction, and interview preparation planning.
```

