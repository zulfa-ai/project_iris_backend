from dataclasses import asdict
from django.db import transaction
from django.utils import timezone

from .models import GameSession, Answer
from .exceptions import NotFound, Conflict, GameplayError
from .providers import BaseScenarioProvider


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
            session = GameSession.objects.create(user=user, topic=topic, status="in_progress")

        scn = self.provider.load(topic)
        current = self.provider.get_current_question(scn, session.current_stage_index, session.current_question_index)
        return {
            "session_id": session.id,
            "status": session.status,
            "total_score": session.total_score,
            "wrong_count": session.wrong_count,
            "current": asdict(current) if current else None,
        }

    def current_state(self, session: GameSession) -> dict:
        scn = self.provider.load(session.topic)
        current = self.provider.get_current_question(scn, session.current_stage_index, session.current_question_index)
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
        # Lock row to prevent race conditions
        session = GameSession.objects.select_for_update().get(id=session.id)

        if session.status != "in_progress":
            raise GameplayError(f"session is {session.status}, cannot answer")

        scn = self.provider.load(session.topic)
        current = self.provider.get_current_question(scn, session.current_stage_index, session.current_question_index)

        if not current:
            session.status = "completed"
            session.ended_at = timezone.now()
            session.ended_reason = "finished"
            session.save(update_fields=["status", "ended_at", "ended_reason"])
            return {"detail": "No more questions. Session completed."}

        q = current.question
        if q.get("id") != question_id:
            raise GameplayError("question_id does not match current question")

        # conflict check (still keep your unique constraint)
        if Answer.objects.filter(session=session, question_id=question_id).exists():
            raise Conflict("already answered")

        # find score
        score_delta = None
        for opt in q.get("options", []):
            if opt.get("text") == selected_text:
                score_delta = int(opt.get("score", 0))
                break
        if score_delta is None:
            raise GameplayError("selected_text not found in options")

        # update session counters
        is_wrong = score_delta < 0
        if is_wrong:
            session.wrong_count += 1
        session.total_score += score_delta

        # advance pointer
        session.current_question_index += 1

        # handle stage boundary
        stages = scn.get("stages", [])
        if session.current_stage_index < len(stages):
            stage_questions = stages[session.current_stage_index].get("questions", [])
            if session.current_question_index >= len(stage_questions):
                session.current_stage_index += 1
                session.current_question_index = 0

        # fail condition
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
            scn, session.current_stage_index, session.current_question_index
        )

        # finish condition
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
            "next": next_current.__dict__ if next_current else None,
            "awarded_points": score_delta,
        }
