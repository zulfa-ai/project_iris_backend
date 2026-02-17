import json
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from .models import GameSession, Answer, StageRun, QuestionRun
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
    Advances session.current_question_index/current_stage_index to the next question.
    Does NOT save session.
    """
    stages = scn.get("stages", [])

    session.current_question_index += 1

    if 0 <= session.current_stage_index < len(stages):
        stage_questions = stages[session.current_stage_index].get("questions", [])
        if session.current_question_index >= len(stage_questions):
            session.current_stage_index += 1
            session.current_question_index = 0


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
    POST body: {"topic":"data_loss"}
    Resumes the latest in-progress session for that topic, else creates a new one.
    """
    topic = request.data.get("topic")
    if not topic:
        return Response({"detail": "topic is required"}, status=status.HTTP_400_BAD_REQUEST)

    created = False

    session = (
        GameSession.objects.filter(user=request.user, topic=topic, status="in_progress")
        .order_by("-started_at")
        .first()
    )

    if not session:
        session = GameSession.objects.create(
            user=request.user,
            topic=topic,
            status="in_progress",
        )
        created = True

    # Load scenario + compute next
    try:
        scn = load_scenario(topic)
    except FileNotFoundError as e:
        return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)

    stage_obj, question_obj = get_stage_and_question(
        scn, session.current_stage_index, session.current_question_index
    )

    payload = {
        "message": "started" if created else "resumed",
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

    stage_obj, question_obj = get_stage_and_question(
        scn, session.current_stage_index, session.current_question_index
    )

    # If pointers are beyond scenario → finish
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

    - Reads the scenario JSON to score the answer
    - Creates StageRun (if needed)
    - Creates QuestionRun snapshot (what the user saw)
    - Creates Answer (linked to QuestionRun)
    - Advances session pointer
    """
    question_id = request.data.get("question_id")
    selected_text = request.data.get("selected_text")

    if not question_id or selected_text is None:
        return Response(
            {"detail": "question_id and selected_text are required"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        session = (
            GameSession.objects.select_for_update()
            .filter(id=session_id, user=request.user)
            .first()
        )
        if not session:
            return Response({"detail": "session not found"}, status=status.HTTP_404_NOT_FOUND)

        if session.status != "in_progress":
            return Response(
                {"detail": f"session is {session.status}, cannot answer"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Duplicate protection (based on stable question_key)
        if Answer.objects.filter(session=session, question_run__question_key=question_id).exists():
            return Response({"detail": "already answered"}, status=status.HTTP_409_CONFLICT)

        # Load scenario JSON
        try:
            scn = load_scenario(session.topic)
        except FileNotFoundError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)

        stage_obj, q_obj = get_stage_and_question(
            scn, session.current_stage_index, session.current_question_index
        )

        if not stage_obj or not q_obj:
            session.status = "completed"
            session.ended_reason = "finished"
            session.ended_at = timezone.now()
            session.save(update_fields=["status", "ended_reason", "ended_at"])
            return Response({"detail": "No more questions. Session completed."}, status=status.HTTP_200_OK)

        # Ensure client answers the current question
        current_qid = q_obj.get("id")
        if current_qid != question_id:
            return Response(
                {"detail": f"question_id does not match current question (expected {current_qid})"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Score the selected option
        score_delta = None
        for opt in q_obj.get("options", []):
            if opt.get("text") == selected_text:
                score_delta = int(opt.get("score", 0))
                break

        if score_delta is None:
            return Response({"detail": "selected_text not found in options"}, status=status.HTTP_400_BAD_REQUEST)

        is_correct = score_delta > 0
        stage_name = stage_obj.get("stage", "")  # e.g. "prepare"

        # StageRun
        stage_run, _ = StageRun.objects.get_or_create(
            session=session,
            stage=stage_name,
            defaults={"order": session.current_stage_index, "status": "active"},
        )
        if stage_run.status != "active":
            stage_run.status = "active"
            stage_run.save(update_fields=["status"])

        # QuestionRun snapshot (what user saw)
        qrun = QuestionRun.objects.create(
            stage_run=stage_run,
            question_key=question_id,
            prompt=q_obj.get("text", ""),
            choices=q_obj.get("options", []),
            order=session.current_question_index,
            status="answered",
            time_limit_seconds=stage_obj.get("time_limit_sec", 30),
        )

        # Answer record
        ans = Answer.objects.create(
            session=session,
            question_run=qrun,
            selected_choice_id=selected_text,  # OK for now; later use a real option id
            selected_text=selected_text,
            score_delta=score_delta,
            is_correct=is_correct,
        )

        # Update scores
        session.total_score += score_delta
        stage_run.stage_score += score_delta

        if not is_correct:
            session.wrong_count += 1

        stage_run.save(update_fields=["stage_score"])

        # Fail condition
        if session.wrong_count >= session.wrong_limit:
            session.status = "failed"
            session.ended_reason = "too_many_wrongs"
            session.ended_at = timezone.now()
            if not session.advice_summary:
                session.advice_summary = (
                    "Too many incorrect answers. Review basics: backups, isolation, reporting, containment, recovery."
                )
            session.save()
            return Response(
                {"answer": AnswerSerializer(ans).data, "session": GameSessionSerializer(session).data, "next": None},
                status=status.HTTP_201_CREATED,
            )

        # Advance pointer
        advance_pointer(scn, session)

        next_stage_obj, next_q_obj = get_stage_and_question(
            scn, session.current_stage_index, session.current_question_index
        )

        # If finished after advance
        if not next_stage_obj or not next_q_obj:
            session.status = "completed"
            session.ended_reason = "finished"
            session.ended_at = timezone.now()

            # mark stage done if you want
            stage_run.status = "done"
            stage_run.save(update_fields=["status"])

            session.save()
            return Response(
                {"answer": AnswerSerializer(ans).data, "session": GameSessionSerializer(session).data, "next": None},
                status=status.HTTP_201_CREATED,
            )

        session.save()

    return Response(
        {
            "answer": AnswerSerializer(ans).data,
            "session": GameSessionSerializer(session).data,
            "next": build_next_payload(next_stage_obj, next_q_obj),
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
