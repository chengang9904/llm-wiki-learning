---
name: fix-wiki-page
description: 修复 Wiki 页面问题的决策树：重读来源 → 最小修改 / 标 deprecated / 报告无法修复。
---

# 修复 Wiki 页面

## 前置

修复必须基于**重读来源**——绝不允许只看 issue 描述就改页面。
先 read_wiki_page 拿全文，再按 verify-sources 技能核实相关引用。

## 按 issue 类型的决策树

- **broken_link**：目标页面存在别名/重定向 → 改写链接；确实无目标 → 解除为纯文本。
- **dead_source_ref**：源文件真没了 → 删除该引用及其支撑的事实；
  只是挪了位置（grep 同名符号）→ 更新为新路径:行号。
- **cite_out_of_range**：grep 符号找到新行号 → 更新；找不到 → 按 dead_source_ref 处理。
- **missing_sources**：能从正文推断来源并核实 → 补引用；推断不出 → 页面标
  status: incomplete 并在未确认事项写明。
- **stale_draft**：核实主要事实后把 status 改为 verified；核实不了就保持 draft
  并报告原因。
- **orphan_page / missing_backlink / index_missing**：属于结构问题，
  修复方式是重跑 s04 postprocess，不要手工加链接——报告「建议重跑后处理」。
- **页面整体失效**（来源全灭、主题已不存在）：标 status: deprecated，
  保留文件，不删除。

## 修改纪律

- **最小修改**：只动与 issue 相关的行，不顺手重写整页；
- 每次修改后更新 frontmatter 的 updated_at，并在修改说明里带 issue id；
- 无法修复的：如实报告原因，更新 issue 状态为 wontfix 而不是硬改。
