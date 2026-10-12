---
name: feedback-security-reviews-sparingly
description: "Заказчик просит реже звать security-analyst — дорого по токенам; ИБ только по реальным триггерам, не на каждый цикл"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 1ebe5d97-269f-4ff6-b1ac-1be2240c6c60
  modified: 2026-09-28T05:55:08.229Z
---

ИБ-проходы (security-analyst, тем более со вторым мнением GPT-6 Astra) звать редко: один раз на новую security-поверхность (T2) и на релиз/инцидент (T3/T5). Повторные проходы после циклов правок по minor/nit не делать — их закрывает обычный code-reviewer; повторный ИБ только если появился новый класс поверхности или ревьюер явно просит.

**Why:** 28.09.2026 заказчик: «Давай ИБ ревью пореже делать по возможности, а то они очень много токенов жрут» — после 8 проходов T2 по Telegram-напоминаниям за два дня.

**How to apply:** перед вызовом security-analyst проверить триггер по скиллу security-gate; если это «перепроверить, закрыты ли minor прошлого прохода» — не звать, отдать code-reviewer. Второе мнение Astra внутри ИБ — только по явной просьбе. См. [[feedback-no-repeat-security-nags]], [[project-astra-cowork]].
