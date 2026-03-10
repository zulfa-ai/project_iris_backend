from dataclasses import asdict
from typing import Dict, List, Optional
import random

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


# =========================================================
# CONSTANTS
# =========================================================

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


# =========================================================
# BASIC SESSION SERVICES
# =========================================================

class SessionService:
    """
    Handles loading / resuming non-AI provider-based sessions.
    """

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


# =========================================================
# ANSWER SUBMISSION SERVICES
# =========================================================

class AnswerService:
    """
    Handles answer submission for provider-based sessions.
    """

    def __init__(self, provider: BaseScenarioProvider):
        self.provider = provider

    @transaction.atomic
    def submit_answer(self, session: GameSession, question_id: str, selected_text: str) -> dict:
        # Lock session row
        session = GameSession.objects.select_for_update().get(id=session.id)

        if session.status != "in_progress":
            raise GameplayError(f"session is {session.status}, cannot answer")

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
            return {"detail": "No more questions. Session completed."}

        q = current.question

        # Supports either "id" or "external_id"
        current_question_id = q.get("id") or q.get("external_id")
        if current_question_id != question_id:
            raise GameplayError("question_id does not match current question")

        if Answer.objects.filter(session=session, question_id=question_id).exists():
            raise Conflict("already answered")

        score_delta = None
        for opt in q.get("options", []):
            if opt.get("text") == selected_text:
                # Supports both "delta_score" and "score"
                score_delta = int(opt.get("delta_score", opt.get("score", 0)))
                break

        if score_delta is None:
            raise GameplayError("selected_text not found in options")

        # Update score / wrong count
        is_wrong = score_delta < 0
        if is_wrong:
            session.wrong_count += 1

        session.total_score += score_delta
        session.current_question_index += 1

        # Move to next stage if current stage questions are exhausted
        stages = scn.get("stages", [])
        if session.current_stage_index < len(stages):
            stage_questions = stages[session.current_stage_index].get("questions", [])
            if session.current_question_index >= len(stage_questions):
                session.current_stage_index += 1
                session.current_question_index = 0

        # Fail condition
        if session.wrong_count >= session.wrong_limit:
            session.status = "failed"
            session.ended_at = timezone.now()
            session.ended_reason = "too_many_wrongs"

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

        # Finish condition
        if session.status == "in_progress" and next_current is None:
            session.status = "completed"
            session.ended_at = timezone.now()
            session.ended_reason = "finished"
            session.save(update_fields=["status", "ended_at", "ended_reason"])

        return {
            "session_id": session.id,
            "status": session.status,
            "ended_reason": session.ended_reason,
            "total_score": session.total_score,
            "wrong_count": session.wrong_count,
            "next": asdict(next_current) if next_current else None,
            "awarded_points": score_delta,
        }


# =========================================================
# PLAYBOOK HELPERS
# =========================================================

def pick_playbook(*, difficulty: str, playbook_slug: str, version: int = 1) -> Playbook:
    """
    Returns a playbook by slug + difficulty + version.
    """
    return Playbook.objects.get(
        slug=playbook_slug,
        difficulty=difficulty,
        version=version,
    )


def get_questions_for_phase(playbook: Playbook, phase: str) -> List[Question]:
    """
    Returns all active questions for a playbook phase.
    """
    return list(
        Question.objects.filter(
            playbook=playbook,
            phase=phase,
            is_active=True,
        ).prefetch_related("options")
    )


def snapshot_questions_to_stage(stage_run: StageRun, questions: List[Question]) -> None:
    """
    Copies Question rows into QuestionRun rows for the session snapshot.
    """
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
    """
    Creates StageRun rows for all phases.
    First stage is active, the rest are locked.
    """
    stage_runs: Dict[str, StageRun] = {}

    for order, phase in enumerate(PHASES_IN_ORDER):
        stage_runs[phase] = StageRun.objects.create(
            session=session,
            stage=STAGE_SLUG_MAP[phase],
            order=order,
            status="active" if order == 0 else "locked",
            stage_score=0,
        )

    return stage_runs


# =========================================================
# STATIC SESSION BUILDER
# =========================================================

@transaction.atomic
def start_static_session(user, difficulty: str, topic: str, questions_per_stage: int = 2) -> GameSession:
    """
    Starts a normal static session:
    - creates GameSession
    - creates StageRuns
    - snapshots DB questions into QuestionRuns
    """
    session = GameSession.objects.create(
        user=user,
        topic=topic,
        difficulty=difficulty,
        status="in_progress",
        total_score=0,
        wrong_count=0,
    )

    playbook = pick_playbook(
        difficulty=difficulty,
        playbook_slug=topic,
    )

    stage_runs = build_stage_runs(session)

    for phase, stage_run in stage_runs.items():
        questions = get_questions_for_phase(playbook, phase)
        random.shuffle(questions)
        selected = questions[:questions_per_stage]
        snapshot_questions_to_stage(stage_run, selected)

    return session


# =========================================================
# AI / HYBRID SESSION HELPERS
# =========================================================

def generate_mock_ai_scenario(topic: str, difficulty: str) -> dict:
    """
    Dynamic AI-style scenario generator.
    Randomises attack storylines and injects.
    """

    phishing_templates = [
        {
            "title": "CEO Impersonation Phishing Attack",
            "brief": "Employees receive an urgent email appearing to come from the CEO requesting immediate verification of company credentials through a login portal.",
            "detect": "Multiple employees report a suspicious email claiming to be from the CEO requesting urgent login verification.",
            "analyse": "Security logs reveal several users clicked the phishing link and attempted to log in.",
            "remediate": "A compromised account begins sending phishing emails internally."
        },
        {
            "title": "Fake Microsoft 365 Login Page",
            "brief": "Employees receive a notification asking them to re-authenticate their Microsoft 365 account through a provided link.",
            "detect": "Users report being redirected to a Microsoft login page after clicking an email link.",
            "analyse": "SOC analysts discover the login page is hosted on a suspicious external domain.",
            "remediate": "One employee account is used to access internal SharePoint files."
        },
        {
            "title": "Payroll Information Phishing",
            "brief": "HR staff receive emails requesting urgent payroll verification due to a supposed payroll processing error.",
            "detect": "The HR team reports multiple emails requesting payroll credential confirmation.",
            "analyse": "Email headers reveal the sender domain was spoofed.",
            "remediate": "Attackers attempt to modify payroll account details."
        },
        {
            "title": "Dropbox Document Phishing",
            "brief": "Employees receive a shared Dropbox document link appearing to contain important internal documents.",
            "detect": "Employees report receiving a Dropbox document from an unknown sender.",
            "analyse": "Security analysis reveals the link redirects to a credential harvesting page.",
            "remediate": "Several users report entering their credentials."
        },
        {
            "title": "SharePoint Document Access Scam",
            "brief": "Users receive a SharePoint notification asking them to review an important document shared by management.",
            "detect": "Employees report unusual SharePoint access notifications.",
            "analyse": "The SharePoint link redirects to a malicious login portal.",
            "remediate": "A compromised user account begins accessing multiple internal documents."
        }
    ]

    ransomware_templates = [
        {
            "title": "Ransomware Outbreak in Finance Department",
            "brief": "Several users report that financial documents are suddenly encrypted with unusual file extensions.",
            "detect": "Users report files being inaccessible in a shared finance drive.",
            "analyse": "Security tools detect mass file encryption activity.",
            "remediate": "The infected workstation attempts to spread laterally."
        }
    ]

    if topic == "phishing":
        scenario = random.choice(phishing_templates)

    elif topic == "ransomware":
        scenario = random.choice(ransomware_templates)

    else:
        return {
            "scenario_title": f"{topic.title()} Incident Simulation",
            "scenario_brief": f"A {difficulty} level {topic} incident simulation has been generated.",
            "injects": []
        }

    return {
        "scenario_title": scenario["title"],
        "scenario_brief": scenario["brief"],
        "injects": [
            {"phase": "Detect", "text": scenario["detect"]},
            {"phase": "Analyse", "text": scenario["analyse"]},
            {"phase": "Remediation", "text": scenario["remediate"]},
        ],
    }


def choose_phase_question_counts(
    ai_scenario: dict,
    default_questions_per_stage: int,
) -> Dict[str, int]:
    """
    Very simple logic:
    - every phase gets at least the default count
    - phases mentioned in AI injects can optionally be increased later

    For now we keep it simple and stable.
    """
    return {phase: default_questions_per_stage for phase in PHASES_IN_ORDER}


# =========================================================
# HYBRID AI SESSION BUILDER
# =========================================================

@transaction.atomic
def start_hybrid_ai_session(user, difficulty: str, topic: str, questions_per_stage: int = 2) -> dict:
    """
    Hybrid AI session:
    - creates session
    - generates AI scenario narrative
    - keeps validated questions from DB playbook
    - snapshots the selected questions into QuestionRuns

    This is the best version for your project right now because:
    - scenario is AI-generated
    - rules/scoring stay grounded in your playbook
    """
    session = GameSession.objects.create(
        user=user,
        topic=topic,
        difficulty=difficulty,
        status="in_progress",
        total_score=0,
        wrong_count=0,
    )

    playbook = pick_playbook(
        difficulty=difficulty,
        playbook_slug=topic,
    )

    ai_scenario = generate_mock_ai_scenario(
        topic=topic,
        difficulty=difficulty,
    )

    stage_runs = build_stage_runs(session)
    phase_question_counts = choose_phase_question_counts(
        ai_scenario=ai_scenario,
        default_questions_per_stage=questions_per_stage,
    )

    for phase, stage_run in stage_runs.items():
        questions = get_questions_for_phase(playbook, phase)
        random.shuffle(questions)

        count = phase_question_counts.get(phase, questions_per_stage)
        selected = questions[:count]

        snapshot_questions_to_stage(stage_run, selected)

    return {
        "session_id": session.id,
        "topic": session.topic,
        "difficulty": session.difficulty,
        "status": session.status,
        "scenario": ai_scenario,
    }