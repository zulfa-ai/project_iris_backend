import json
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from .models import GameSession, Answer
from .serializers import GameSessionSerializer, AnswerSerializer


# -----------------------------
# Scenario helpers
# -----------------------------
BASE_DIR = Path(__file__).resolve().parent.parent  # backend/


def load_scenario(topic: str) -> dict:
    scenario_path = BASE_DIR / "scenarios" / "data" / f"{topic}.json"
    if not scenario_path.exists():
        raise FileNotFoundError(f"Scenario file not found: {scenario_path}")
    return json.loads(scenario_path.read_text(encoding="utf-8"))


def get_stage_and_question(scn: dict, stage_index: int, question_index: int):
    """
    Returns (stage_obj, question_obj) or (None, None) if out of range.
    """
    stages = scn.get("stages", [])
    if stage_index < 0 or stage_index >= len(stages):
        return None, None
    stage_obj = stages[stage_index]
    questions = stage_obj.get("questions", [])
    if question_index < 0 or question_index >= len(questions):
        return stage_obj, None
    return stage_obj, questions[question_index]


def build_next_payload(stage_obj: dict, question_obj: dict) -> dict:
    return {
        "stage": stage_obj.get("stage"),
        "time_limit_sec": stage_obj.get("time_limit_sec", 30),
        "question": question_obj,
    }


def advance_pointer(scn: dict, session: GameSession) -> None:
    """
    Advances session.current_question_index / current_stage_index to the next valid question.
    Does NOT save session.
    """
    stages = scn.get("stages", [])

    # Move to next question
    session.current_question_index += 1

    # If stage exists, check if question index exceeded stage length → move to next stage
    if 0 <= session.current_stage_index < len(stages):
        stage_questions = stages[session.current_stage_index].get("questions", [])
        if session.current_question_index >= len(stage_questions):
            session.current_stage_index += 1
            session.current_question_index = 0


def is_finished(scn: dict, session: GameSession) -> bool:
    stages = scn.get("stages", [])
    if session.current_stage_index >= len(stages):
        return True
    stage_obj = stages[session.current_stage_index]
    questions = stage_obj.get("questions", [])
    if session.current_question_index >= len(questions):
        # if the current stage has no questions (or we've advanced badly), treat as finished
        # (you can also call advance_pointer loop if you want to skip empty stages)
        return False
    return False


# -----------------------------
# Endpoints
# -----------------------------
@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    return Response({"ok": True, "service": "gameplay"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def start_or_resume(request):
    """
    POST body: {"topic":"ransomware"}
    Resumes the latest in-progress session for that topic, else creates a new one.
    """
    topic = request.data.get("topic")
    if not topic:
        return Response({"detail": "topic is required"}, status=status.HTTP_400_BAD_REQUEST)

    session = (
        GameSession.objects.filter(user=request.user, topic=topic, status="in_progress")
        .order_by("-started_at")
        .first()
    )

    if not session:
        session = GameSession.objects.create(user=request.user, topic=topic, status="in_progress")

    # Return session + next question payload (if any)
    try:
        scn = load_scenario(topic)
    except FileNotFoundError as e:
        return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)

    stage_obj, question_obj = get_stage_and_question(scn, session.current_stage_index, session.current_question_index)

    payload = {
        "message": "resumed" if session.started_at and session.answers.exists() else "started",
        "session": GameSessionSerializer(session).data,
        "next": build_next_payload(stage_obj, question_obj) if (stage_obj and question_obj) else None,
    }
    return Response(payload, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def current_state(request, session_id: int):
    session = GameSession.objects.filter(id=session_id, user=request.user).first()
    if not session:
        return Response({"detail": "session not found"}, status=status.HTTP_404_NOT_FOUND)

    if session.status != "in_progress":
        return Response({"session": GameSessionSerializer(session).data, "next": None})

    try:
        scn = load_scenario(session.topic)
    except FileNotFoundError as e:
        return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)

    stage_obj, question_obj = get_stage_and_question(scn, session.current_stage_index, session.current_question_index)

    # If pointers are beyond the scenario, complete the session
    if not stage_obj or not question_obj:
        session.status = "completed"
        session.ended_reason = "finished"
        session.ended_at = timezone.now()
        session.save(update_fields=["status", "ended_reason", "ended_at"])
        return Response({"session": GameSessionSerializer(session).data, "next": None, "message": "finished"})

    return Response(
        {
            "session": GameSessionSerializer(session).data,
            "next": build_next_payload(stage_obj, question_obj),
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit_answer(request, session_id: int):
    """
    POST body:
    {
      "question_id": "prep-1",
      "selected_text": "Yes"
    }
    Server computes score/is_correct from scenario JSON.
    """
    question_id = request.data.get("question_id")
    selected_text = request.data.get("selected_text")

    if not question_id or selected_text is None:
        return Response(
            {"detail": "question_id and selected_text are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        # Lock the session row to prevent race conditions/double score
        session = (
            GameSession.objects.select_for_update()
            .filter(id=session_id, user=request.user)
            .first()
        )

        if not session:
            return Response({"detail": "session not found"}, status=status.HTTP_404_NOT_FOUND)

        if session.status != "in_progress":
            return Response({"detail": f"session is {session.status}, cannot answer"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            scn = load_scenario(session.topic)
        except FileNotFoundError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)

        stage_obj, q_obj = get_stage_and_question(scn, session.current_stage_index, session.current_question_index)
        if not stage_obj or not q_obj:
            # no more questions
            session.status = "completed"
            session.ended_reason = "finished"
            session.ended_at = timezone.now()
            session.save(update_fields=["status", "ended_reason", "ended_at"])
            return Response({"detail": "No more questions. Session completed."}, status=status.HTTP_200_OK)

        # Ensure client is answering the current question
        current_qid = q_obj.get("id")
        if current_qid != question_id:
            return Response({"detail": "question_id does not match current question"}, status=status.HTTP_400_BAD_REQUEST)

        # Prevent duplicate answers
        if Answer.objects.filter(session=session, question_id=question_id).exists():
            return Response({"detail": "already answered"}, status=status.HTTP_409_CONFLICT)

        # Compute score from scenario options (SERVER SIDE)
        score_delta = None
        for opt in q_obj.get("options", []):
            if opt.get("text") == selected_text:
                score_delta = int(opt.get("score", 0))
                break

        if score_delta is None:
            return Response({"detail": "selected_text not found in options"}, status=status.HTTP_400_BAD_REQUEST)

        is_correct = score_delta > 0
        stage_name = stage_obj.get("stage", "")

        # Record answer
        ans = Answer.objects.create(
            session=session,
            stage=stage_name,
            question_id=question_id,
            selected_text=selected_text,
            score_delta=score_delta,
            is_correct=is_correct,
        )

        # Update session totals
        session.total_score += score_delta
        if not is_correct:
            session.wrong_count += 1

        # Fail condition
        if session.wrong_count >= session.wrong_limit:
            session.status = "failed"
            session.ended_reason = "too_many_wrongs"
            session.ended_at = timezone.now()
            # Optional: summary
            if not session.advice_summary:
                session.advice_summary = (
                    "Too many incorrect answers. Review basics: backups, isolation, reporting, containment, recovery."
                )
            session.save()
            return Response(
                {"answer": AnswerSerializer(ans).data, "session": GameSessionSerializer(session).data, "next": None},
                status=status.HTTP_201_CREATED,
            )

        # Advance pointers to next question/stage
        advance_pointer(scn, session)

        # If out of questions after advancing, complete
        next_stage_obj, next_q_obj = get_stage_and_question(
            scn, session.current_stage_index, session.current_question_index
        )
        if not next_stage_obj or not next_q_obj:
            session.status = "completed"
            session.ended_reason = "finished"
            session.ended_at = timezone.now()
            session.save()
            return Response(
                {"answer": AnswerSerializer(ans).data, "session": GameSessionSerializer(session).data, "next": None},
                status=status.HTTP_201_CREATED,
            )

        session.save()

    # build next payload outside the lock
    next_payload = build_next_payload(next_stage_obj, next_q_obj)

    return Response(
        {
            "answer": AnswerSerializer(ans).data,
            "session": GameSessionSerializer(session).data,
            "next": next_payload,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def quit_session(request, session_id: int):
    session = GameSession.objects.filter(id=session_id, user=request.user).first()
    if not session:
        return Response({"detail": "session not found"}, status=status.HTTP_404_NOT_FOUND)

    if session.status == "in_progress":
        session.status = "abandoned"
        session.ended_reason = "user_quit"
        session.ended_at = timezone.now()
        session.save(update_fields=["status", "ended_reason", "ended_at"])

    return Response({"session": GameSessionSerializer(session).data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def history(request):
    qs = GameSession.objects.filter(user=request.user).order_by("-started_at")[:50]
    return Response({"sessions": GameSessionSerializer(qs, many=True).data})
