---
name: harness
description: Planner → Generator → Evaluator の3エージェントハーネスで機能追加・変更を計画・実装・検証する。/harness <依頼内容> で明示的に起動する。
argument-hint: [依頼内容]
disable-model-invocation: true
---

Anthropic のハーネス設計(Planner → Generator → Evaluator)に基づき、次の依頼を進める: $ARGUMENTS

## 原則

- エージェント間の連携はファイル経由で行う(`plan.md` / `contract.md` / `evaluation.md`)
- 自己評価バイアスを避けるため、Generator は合否判定をせず、Evaluator は実装を修正しない
- 契約基準を 1 つでも下回ればスプリント失敗として反復する

## 手順

1. **準備**: セッションのスクラッチパッド配下に `harness/run-<連番>/` を作成し、以後 RUN_DIR とする
2. **計画**: Agent(subagent_type: "planner") に依頼内容と RUN_DIR の絶対パスを渡す → `RUN_DIR/plan.md`
3. **ユーザー承認**: plan.md の要点(スコープ・設計・エッジケース)を提示し、承認を得てから先へ進む
4. **契約提案**: Agent("generator") へ「モード 1(契約提案): RUN_DIR/plan.md を読み contract.md を提案」
5. **契約レビュー**: Agent("evaluator") へ「モード 1(契約レビュー): contract.md を審査し確定版へ更新」
6. **実装**: Agent("generator") へ「モード 2(実装): plan.md と確定版 contract.md に従い実装。evaluation.md があればその不合格の解消を最優先」
7. **評価**: Agent("evaluator") へ「モード 2(評価): 全基準を実行検証し evaluation.md に判定を書く」
8. **反復**: 不合格があれば 6→7 を繰り返す(最大 3 ラウンド。超えたら状況を整理してユーザーへ相談)
9. **完了報告**: 変更ファイル・検証結果・残課題を要約して報告する。コミットはユーザーの承認を得てから行う
