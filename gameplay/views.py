import random

from django.db import transaction
from django.utils import timezone
from django.shortcuts import get_object_or_404

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from .models import GameSession, Answer, QuestionRun
from .serializers import GameSessionSerializer, AnswerSerializer

from .services_new import (
    start_hybrid_ai_session,
    generate_ai_inject,
    generate_ai_crisis_event,
)


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    return Response({"ok": True})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def session_start(request):
    difficulty = request.data.get("difficulty")
    topic = request.data.get("topic") or request.data.get("playbook")
    questions_per_stage = int(request.data.get("questions_per_stage", 2))

    if not difficulty or not topic:
        return Response(
            {"detail": "Missing difficulty or topic."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        result = start_hybrid_ai_session(
            user=request.user,
            difficulty=difficulty,
            topic=topic,
            questions_per_stage=questions_per_stage,
        )
        return Response(result, status=status.HTTP_201_CREATED)

    except Exception as e:
        return Response(
            {"detail": str(e)},
            status=status.HTTP_400_BAD_REQUEST,
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def current_state(request, session_id):
    session = get_object_or_404(GameSession, id=session_id, user=request.user)

    if session.status != "in_progress":
        return Response({
            "session": GameSessionSerializer(session).data,
            "next": None,
        })

    stage_run = session.stages.filter(status="active").order_by("order").first()

    if not stage_run:
        return Response({
            "session": GameSessionSerializer(session).data,
            "next": None,
        })

    qrun = stage_run.questions.filter(status="pending").order_by("order").first()

    if not qrun:
        return Response({
            "session": GameSessionSerializer(session).data,
            "next": None,
        })

    base_time = qrun.time_limit_seconds or 30
    adjusted_time = max(8, base_time - (session.pressure_level // 20))

    severity = "low"
    if session.pressure_level >= 80:
        severity = "critical"
    elif session.pressure_level >= 60:
        severity = "high"
    elif session.pressure_level >= 40:
        severity = "elevated"

    return Response({
        "session": GameSessionSerializer(session).data,
        "next": {
            "stage": stage_run.stage,
            "time_limit_sec": adjusted_time,
            "question": {
                "id": qrun.question_key,
                "text": qrun.prompt,
                "options": qrun.choices,
            }
        },
        "pressure_level": session.pressure_level,
        "severity": severity,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit_answer(request, session_id):
    session = get_object_or_404(GameSession, id=session_id, user=request.user)

    if session.status != "in_progress":
        return Response({"detail": f"session is {session.status}"}, status=400)

    question_id = request.data.get("question_id")
    selected_choice_id = request.data.get("selected_choice_id")

    if not question_id or not selected_choice_id:
        return Response({"detail": "Missing parameters"}, status=400)

    qrun = QuestionRun.objects.select_related("stage_run").filter(
        stage_run__session=session,
        question_key=question_id,
        status="pending",
    ).first()

    if not qrun:
        return Response({"detail": "Question not found or already answered"}, status=404)

    score_delta = None
    selected_text = ""

    for opt in qrun.choices:
        if str(opt.get("id")) == str(selected_choice_id):
            score_delta = int(opt.get("delta_score", 0))
            selected_text = opt.get("text", "")
            break

    if score_delta is None:
        return Response({"detail": "Invalid option"}, status=400)

    is_correct = score_delta > 0

    with transaction.atomic():
        session = GameSession.objects.select_for_update().get(id=session.id)
        qrun = QuestionRun.objects.select_for_update().get(id=qrun.id)
        stage_run = qrun.stage_run

        qrun.status = "answered"
        qrun.save(update_fields=["status"])

        ans = Answer.objects.create(
            session=session,
            question_run=qrun,
            selected_choice_id=str(selected_choice_id),
            selected_text=selected_text,
            score_delta=score_delta,
            is_correct=is_correct,
        )

        session.total_score += score_delta
        stage_run.stage_score += score_delta

        if not is_correct:
            session.wrong_count += 1

        if score_delta > 0:
            session.pressure_level = max(0, session.pressure_level - 5)
        elif score_delta == -5:
            session.pressure_level += 10
        elif score_delta <= -10:
            session.pressure_level += 20

        session.pressure_level = min(session.pressure_level, 100)

        severity = "low"
        if session.pressure_level >= 80:
            severity = "critical"
        elif session.pressure_level >= 60:
            severity = "high"
        elif session.pressure_level >= 40:
            severity = "elevated"

        inject_message = None
        if random.random() < 0.4:
            inject_message = generate_ai_inject(session.topic, severity)

        crisis_event = None
        if severity in ["high", "critical"] and random.random() < 0.3:
            crisis_event = generate_ai_crisis_event(session.topic, severity)

        if session.pressure_level >= 100:
            session.status = "failed"
            session.ended_reason = "system_escalation"
            session.ended_at = timezone.now()
        elif session.wrong_count >= session.wrong_limit:
            session.status = "failed"
            session.ended_reason = "too_many_wrongs"
            session.ended_at = timezone.now()

        if session.status != "failed":
            if not stage_run.questions.filter(status="pending").exists():
                stage_run.status = "done"
                stage_run.save(update_fields=["status"])

                next_stage = session.stages.filter(status="locked").order_by("order").first()

                if next_stage:
                    next_stage.status = "active"
                    next_stage.save(update_fields=["status"])
                else:
                    session.status = "completed"
                    session.ended_reason = "finished"
                    session.ended_at = timezone.now()

        stage_run.save(update_fields=["stage_score"])
        session.save()

    return Response({
        "answer": AnswerSerializer(ans).data,
        "session": GameSessionSerializer(session).data,
        "severity": severity,
        "ai_inject": inject_message,
        "crisis_event": crisis_event,
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def history(request):
    qs = GameSession.objects.filter(user=request.user).order_by("-started_at")[:50]
    return Response({"sessions": GameSessionSerializer(qs, many=True).data})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def quit_session(request, session_id):
    session = GameSession.objects.filter(id=session_id, user=request.user).first()

    if not session:
        return Response({"detail": "session not found"}, status=404)

    if session.status == "in_progress":
        session.status = "abandoned"
        session.ended_reason = "user_quit"
        session.ended_at = timezone.now()
        session.save(update_fields=["status", "ended_reason", "ended_at"])

    return Response({"session": GameSessionSerializer(session).data})