"""Test support: builds synthetic Claude Code session files mimicking the real layout (spec 7.2)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

T0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)


class SessionBuilder:
    def __init__(
        self, cwd: str, session_id: str = "sess-1", branch: str = "main", start: datetime = T0
    ) -> None:
        self.start = start
        self.cwd = cwd
        self.session_id = session_id
        self.branch = branch
        self.rows: list[dict[str, Any]] = []
        self._n = 0
        self._last: str | None = None

    def _tick(self) -> str:
        self._n += 1
        return (self.start + timedelta(minutes=self._n)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def _row(self, kind: str, message: dict[str, Any], **extra: Any) -> dict[str, Any]:
        uuid = f"u{len(self.rows) + 1}"
        row: dict[str, Any] = {
            "parentUuid": self._last,
            "isSidechain": False,
            "type": kind,
            "message": message,
            "uuid": uuid,
            "timestamp": self._tick(),
            "cwd": self.cwd,
            "sessionId": self.session_id,
            "version": "2.1.288",
            "gitBranch": self.branch,
            **extra,
        }
        self.rows.append(row)
        self._last = uuid
        return row

    def human(self, text: str, **extra: Any) -> str:
        row = self._row(
            "user", {"role": "user", "content": text}, origin={"kind": "human"}, **extra
        )
        return str(row["uuid"])

    def raw_user(self, content: Any, **extra: Any) -> None:
        self._row("user", {"role": "user", "content": content}, **extra)

    def assistant_text(self, text: str, message_id: str = "m1") -> None:
        self._row(
            "assistant",
            {"role": "assistant", "id": message_id, "content": [{"type": "text", "text": text}]},
        )

    def tool(
        self,
        name: str,
        tool_input: dict[str, Any],
        *,
        tool_id: str,
        error: bool = False,
        denial: str | None = None,
        message_id: str = "m2",
        **extra: Any,
    ) -> None:
        self._row(
            "assistant",
            {
                "role": "assistant",
                "id": message_id,
                "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}],
            },
            **extra,
        )
        result: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": "SECRET FILE CONTENT",
        }
        if error or denial:
            result["is_error"] = True
        self._row(
            "user",
            {"role": "user", "content": [result]},
            toolUseResult={"originalFile": "SECRET FILE CONTENT"},
            sourceToolAssistantUUID=self._last,
            **({"toolDenialKind": denial} if denial else {}),
        )

    def compact(self) -> None:
        previous = self._last
        row = {
            "parentUuid": None,
            "logicalParentUuid": previous,
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": f"u{len(self.rows) + 1}",
            "timestamp": self._tick(),
            "sessionId": self.session_id,
        }
        self.rows.append(row)
        self._last = str(row["uuid"])

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(r) for r in self.rows]
        lines.insert(1, "this is not json")  # malformed lines must be skipped
        lines.insert(2, json.dumps({"type": "totally-new-type", "x": 1}))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
