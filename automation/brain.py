# -*- coding: utf-8 -*-
"""采配ブレーン — Claude APIによる次タスクの采配(オプション)。

ANTHROPIC_API_KEY が設定されていれば、現在のオフィス状態と采配盤をClaudeに渡し、
「誰に何を割り当てるか / 人間に聞くべきことは何か」の判断を構造化出力で受け取る。
未設定・失敗時は None を返し、オーケストレータはルールベース采配にフォールバックする。
"""
import json
import os

MODEL = "claude-opus-4-8"

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "description": "新しく作業を割り当てるメンバー(既に作業中の者は含めない)",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "TEAMのnameと完全一致"},
                    "task": {"type": "string", "description": "作業内容の一文"},
                    "hours": {"type": "integer", "description": "作業時間(1〜4)"},
                },
                "required": ["name", "task", "hours"],
                "additionalProperties": False,
            },
        },
        "log_lines": {
            "type": "array",
            "description": "業務日誌に追記する行(「名前: 内容」形式、時刻は不要)",
            "items": {"type": "string"},
        },
        "escalations": {
            "type": "array",
            "description": "人間(社長)の判断が必要な事項。無ければ空配列",
            "items": {"type": "string"},
        },
    },
    "required": ["assignments", "log_lines", "escalations"],
    "additionalProperties": False,
}

SYSTEM = """あなたは株式会社followのAIバーチャルオフィスのCOO「ジン」です。
采配盤(JOHN_TASKBOARD)と現在の稼働状態を見て、手空きのメンバーに次のタスクを割り当てます。

ルール:
- メンバー名は必ずチームリストと完全一致させる
- 既に作業中(working)のメンバーには割り当てない
- 采配盤に根拠のないタスクを発明しない。進捗100%のタスクは割り当てない
- タスクに「依存:名前」が付いている場合、その名前の担当のP0〜P3タスクが完了(100%)するまで割り当てない
- 社長決裁が必要な事項(P0の社長タスク、予算、外部への新規送信)は assignments にせず escalations に入れる
- 常時稼働(待機ゼロ): 実行時期が先のタスク(P4-P5)しか持たないメンバーには、その本番に
  向けた先行準備・調査タスクを自動創出して割り当ててよい。あなたが割り当てなかった
  手空きメンバーには、オーケストレータが「ロール別バックログ」から即座に
  定常業務・自主研究タスクを采配する — 停止するエージェントは存在しない
- したがって assignments を無理に埋める必要はない(空でも常時稼働レイヤーが埋める)"""


def decide(team_names, state, taskboard):
    """采配を返す。dict(DECISION_SCHEMA準拠) か None(利用不可/失敗)。"""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return None
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": DECISION_SCHEMA},
            },
            system=SYSTEM,
            messages=[{
                "role": "user",
                "content": (
                    "チームメンバー一覧:\n" + json.dumps(team_names, ensure_ascii=False)
                    + "\n\n現在の office_state.json:\n"
                    + json.dumps(state, ensure_ascii=False, indent=1)
                    + "\n\n采配盤 JOHN_TASKBOARD.md:\n" + taskboard
                    + "\n\n次の采配を決めてください。"
                ),
            }],
        )
        if response.stop_reason == "refusal":
            return None
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)
    except Exception:
        return None
