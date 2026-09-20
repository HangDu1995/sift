
def dispatch_sequence(user_task: str, pairs: list[tuple[str, str, str, str]], closing: str) -> list[dict]:
    recs: list[dict] = [{"message": {"role": "user", "content": [{"type": "text", "text": user_task}]}}]
    for tid, role, prompt, result in pairs:
        recs.append(
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tid,
                            "name": "Task",
                            "input": {"subagent_type": role, "prompt": prompt},
                        }
                    ],
                }
            }
        )
        recs.append(
            {"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": result}]}}
        )
    recs.append({"message": {"role": "assistant", "content": [{"type": "text", "text": closing}]}})
    return recs
