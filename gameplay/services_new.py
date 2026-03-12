from dataclasses import asdict
from typing import Dict, List
import json
import random
import requests

from django.db import transaction
from django.utils import timezone

from .models import (
    GameSession,
    Answer,
    StageRun,
    QuestionRun,
    Playbook,
    Question,
)
from .exceptions import Conflict, GameplayError
from .providers import BaseScenarioProvider


PHASES_IN_ORDER = [
    "Prepare",
    "Detect",
    "Analyse",
    "Remediation",
    "Post-Incident",
]

STAGE_SLUG_MAP = {
    "Prepare": "prepare",
    "Detect": "detect",
    "Analyse": "analyse",
    "Remediation": "remediate",
    "Post-Incident": "post_incident",
}


class SessionService:
    def __init__(self, provider: BaseScenarioProvider):
        self.provider = provider

    def start_or_resume(self, user, topic: str) -> dict:
        if not topic:
            raise GameplayError("topic is required")

        session = (
            GameSession.objects
            .filter(user=user, topic=topic, status="in_progress")
            .order_by("-started_at")
            .first()
        )

        if not session:
            session = GameSession.objects.create(
                user=user,
                topic=topic,
                status="in_progress",
            )

        scn = self.provider.load(topic)
        current = self.provider.get_current_question(
            scn,
            session.current_stage_index,
            session.current_question_index,
        )

        return {
            "session_id": session.id,
            "status": session.status,
            "total_score": session.total_score,
            "wrong_count": session.wrong_count,
            "current": asdict(current) if current else None,
        }

    def current_state(self, session: GameSession) -> dict:
        scn = self.provider.load(session.topic)
        current = self.provider.get_current_question(
            scn,
            session.current_stage_index,
            session.current_question_index,
        )

        return {
            "session_id": session.id,
            "topic": session.topic,
            "status": session.status,
            "total_score": session.total_score,
            "wrong_count": session.wrong_count,
            "current": asdict(current) if current else None,
        }


class AnswerService:
    def __init__(self, provider: BaseScenarioProvider):
        self.provider = provider

    @transaction.atomic
    def submit_answer(self, session: GameSession, question_id: str, selected_text: str) -> dict:
        session = GameSession.objects.select_for_update().get(id=session.id)

        if session.status != "in_progress":
            raise GameplayError(f"Session is {session.status}")

        scn = self.provider.load(session.topic)
        current = self.provider.get_current_question(
            scn,
            session.current_stage_index,
            session.current_question_index,
        )

        if not current:
            session.status = "completed"
            session.ended_at = timezone.now()
            session.ended_reason = "finished"
            session.save(update_fields=["status", "ended_at", "ended_reason"])
            return {"detail": "Session completed"}

        q = current.question
        current_question_id = q.get("id") or q.get("external_id")

        if current_question_id != question_id:
            raise GameplayError("question_id mismatch")

        if Answer.objects.filter(session=session, question_id=question_id).exists():
            raise Conflict("Question already answered")

        score_delta = None
        for opt in q.get("options", []):
            if opt.get("text") == selected_text:
                score_delta = int(opt.get("delta_score", 0))
                break

        if score_delta is None:
            raise GameplayError("Selected option not found")

        if score_delta < 0:
            session.wrong_count += 1

        session.total_score += score_delta
        session.current_question_index += 1

        stages = scn.get("stages", [])
        if session.current_stage_index < len(stages):
            stage_questions = stages[session.current_stage_index].get("questions", [])
            if session.current_question_index >= len(stage_questions):
                session.current_stage_index += 1
                session.current_question_index = 0

        if session.wrong_count >= session.wrong_limit:
            session.status = "failed"
            session.ended_reason = "too_many_wrongs"
            session.ended_at = timezone.now()

        session.save()

        Answer.objects.create(
            session=session,
            stage=current.stage,
            question_id=question_id,
            selected_text=selected_text,
            score_delta=score_delta,
            is_correct=(score_delta > 0),
        )

        next_current = self.provider.get_current_question(
            scn,
            session.current_stage_index,
            session.current_question_index,
        )

        if session.status == "in_progress" and next_current is None:
            session.status = "completed"
            session.ended_reason = "finished"
            session.ended_at = timezone.now()
            session.save(update_fields=["status", "ended_reason", "ended_at"])

        return {
            "session_id": session.id,
            "status": session.status,
            "total_score": session.total_score,
            "wrong_count": session.wrong_count,
            "awarded_points": score_delta,
            "next": asdict(next_current) if next_current else None,
        }


def pick_playbook(*, difficulty: str, playbook_slug: str, version: int = 1) -> Playbook:
    return Playbook.objects.get(
        slug=playbook_slug,
        difficulty=difficulty,
        version=version,
    )


def get_questions_for_phase(playbook: Playbook, phase: str) -> List[Question]:
    return list(
        Question.objects
        .filter(playbook=playbook, phase=phase, is_active=True)
        .prefetch_related("options")
    )


def snapshot_questions_to_stage(stage_run: StageRun, questions: List[Question]):
    for q_order, q in enumerate(questions):
        QuestionRun.objects.create(
            stage_run=stage_run,
            question_key=q.external_id,
            prompt=q.prompt,
            choices=[
                {
                    "id": opt.label,
                    "label": opt.label,
                    "text": opt.text,
                    "delta_score": opt.delta_score,
                }
                for opt in q.options.all()
            ],
            order=q_order,
        )


def build_stage_runs(session: GameSession) -> Dict[str, StageRun]:
    stage_runs = {}

    for order, phase in enumerate(PHASES_IN_ORDER):
        stage_runs[phase] = StageRun.objects.create(
            session=session,
            stage=STAGE_SLUG_MAP[phase],
            order=order,
            status="active" if order == 0 else "locked",
            stage_score=0,
        )

    return stage_runs


def generate_ai_scenario(topic: str, difficulty: str) -> dict:
    prompt = f"""
Generate a cyber incident training scenario.

Incident type: {topic}
Difficulty: {difficulty}

Return ONLY valid JSON.

Format:
{{
  "scenario_title": "",
  "scenario_brief": "",
  "injects": [
    {{"phase":"Detect","text":""}},
    {{"phase":"Analyse","text":""}},
    {{"phase":"Remediation","text":""}}
  ]
}}
""".strip()

    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": "llama3:latest",
                "prompt": prompt,
                "stream": False,
            },
            timeout=60,
        )
        response.raise_for_status()

        data = response.json()
        raw = data.get("response", "").strip()

        if raw.startswith("```json"):
            raw = raw[len("```json"):].strip()
        if raw.startswith("```"):
            raw = raw[len("```"):].strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()

        return json.loads(raw)

    except Exception as e:
        print(f"[AI scenario fallback triggered] {e}")
        return {
            "scenario_title": f"{topic.title()} Incident",
            "scenario_brief": f"A {difficulty} level {topic} incident has been detected.",
            "injects": [
                {"phase": "Detect", "text": "Initial suspicious activity reported."},
                {"phase": "Analyse", "text": "Security team begins investigation."},
                {"phase": "Remediation", "text": "Containment actions initiated."},
            ],
        }


def generate_ai_inject(topic: str, severity: str):
    prompt = f"""
Generate a short cyber incident update.

Topic: {topic}
Severity: {severity}

Return ONLY valid JSON.

Format:
{{ "inject": "" }}
""".strip()

    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": "llama3:latest",
                "prompt": prompt,
                "stream": False,
            },
            timeout=60,
        )
        response.raise_for_status()

        data = response.json()
        raw = data.get("response", "").strip()

        if raw.startswith("```json"):
            raw = raw[len("```json"):].strip()
        if raw.startswith("```"):
            raw = raw[len("```"):].strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()

        parsed = json.loads(raw)
        return parsed.get("inject")

    except Exception as e:
        print(f"[AI inject fallback triggered] {e}")
        return None


def generate_ai_crisis_event(topic: str, severity: str):
    prompt = f"""
You are generating a sudden cyber crisis escalation event.

Incident type: {topic}
Severity: {severity}

Return ONLY valid JSON.
Do not include markdown fences.
Do not include explanations.

Format:
{{
  "crisis_event": "short unexpected escalation message"
}}

Rules:
- Keep it realistic
- Make it feel urgent
- Keep it under 25 words
- The event must match the incident type
""".strip()

    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": "llama3:latest",
                "prompt": prompt,
                "stream": False,
            },
            timeout=60,
        )
        response.raise_for_status()

        data = response.json()
        raw = data.get("response", "").strip()

        if raw.startswith("```json"):
            raw = raw[len("```json"):].strip()
        if raw.startswith("```"):
            raw = raw[len("```"):].strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()

        parsed = json.loads(raw)
        return parsed.get("crisis_event")

    except Exception as e:
        print(f"[AI crisis fallback triggered] {e}")
        return None


@transaction.atomic
def start_hybrid_ai_session(user, difficulty: str, topic: str, questions_per_stage: int = 2) -> dict:
    session = GameSession.objects.create(
        user=user,
        topic=topic,
        difficulty=difficulty,
        status="in_progress",
        total_score=0,
        wrong_count=0,
        pressure_level=0,
    )

    playbook = pick_playbook(
        difficulty=difficulty,
        playbook_slug=topic,
    )

    ai_scenario = generate_ai_scenario(
        topic=topic,
        difficulty=difficulty,
    )

    stage_runs = build_stage_runs(session)

    for phase, stage_run in stage_runs.items():
        questions = get_questions_for_phase(playbook, phase)
        random.shuffle(questions)
        selected = questions[:questions_per_stage]
        snapshot_questions_to_stage(stage_run, selected)

    return {
        "session_id": session.id,
        "topic": session.topic,
        "difficulty": session.difficulty,
        "status": session.status,
        "scenario": ai_scenario,
    }