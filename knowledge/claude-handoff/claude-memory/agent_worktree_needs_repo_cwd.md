---
name: agent-worktree-needs-repo-cwd
description: "Agent tool с isolation \"worktree\" создаёт дерево из cwd СЕССИИ; после любого cd в сторону cwd сбрасывается в ~/Документы/Astra Cowork (не git) и запуск падает"
metadata: 
  node_type: memory
  type: project
  originSessionId: dacfef23-cfb4-4306-ba11-ff1381c31710
  modified: 2026-09-16T13:53:21.748Z
---

`Agent(isolation: "worktree")` создаёт worktree из текущего каталога сессии. В этом проекте
cwd после любого `cd` в другой каталог (например, в `~/Документы/yurka-team` ради коммита
правил) сбрасывается харнессом в `/home/astra/Документы/Astra Cowork` — родительскую папку,
которая **не git-репозиторий**. Следующий запуск worktree-агента падает:
`Cannot create agent worktree: not in a git repository`.

**Why:** 16.09 запуск #46 сорвался ровно так после коммита в HQ-репо; бриф пришлось
отправлять заново целиком.

**How to apply:** перед каждым `Agent(isolation: "worktree")` — отдельный Bash
`cd "/home/astra/Документы/Astra Cowork/astra-cowork" && git rev-parse --show-toplevel`,
и только потом запуск. Агентов без worktree (ревьюеры, отладчик) это не касается. Связано:
[[project-astra-cowork]].
