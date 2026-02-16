import json
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Dict, Any


@dataclass
class CurrentQuestion:
    topic: str
    stage_index: int
    question_index: int
    stage: str
    time_limit_sec: int
    question: Dict[str, Any]  # {id,text,options:[{id/text/score...}]}


class BaseScenarioProvider:
    def load(self, topic: str) -> dict:
        raise NotImplementedError

    def get_current_question(self, scn: dict, s_idx: int, q_idx: int) -> Optional[CurrentQuestion]:
        raise NotImplementedError


class JsonScenarioProvider(BaseScenarioProvider):
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir

    def load(self, topic: str) -> dict:
        fp = self.base_dir / "scenarios" / "data" / f"{topic}.json"
        if not fp.exists():
            raise FileNotFoundError(f"Scenario file not found: {fp}")
        return json.loads(fp.read_text(encoding="utf-8"))

    def get_current_question(self, scn: dict, s_idx: int, q_idx: int) -> Optional[CurrentQuestion]:
        stages = scn.get("stages", [])
        if s_idx >= len(stages):
            return None
        stage = stages[s_idx]
        questions = stage.get("questions", [])
        if q_idx >= len(questions):
            return None

        q = questions[q_idx]
        return CurrentQuestion(
            topic=scn.get("topic", ""),
            stage_index=s_idx,
            question_index=q_idx,
            stage=stage.get("stage", ""),
            time_limit_sec=stage.get("time_limit_sec", 30),
            question={
                "id": q.get("id"),
                "text": q.get("question"),
                "options": q.get("options", []),
            },
        )