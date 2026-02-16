from rest_framework import serializers
from .models import GameSession, Answer


class AnswerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Answer
        fields = [
            "id",
            "session",
            "stage",
            "question_id",
            "selected_text",
            "score_delta",
            "is_correct",
            "answered_at",
        ]
        read_only_fields = ["id", "answered_at"]


class GameSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = GameSession
        fields = [
            "id",
            "user",
            "topic",
            "started_at",
            "ended_at",
            "status",
            "ended_reason",
            "last_activity_at",
            "current_stage_index",
            "current_question_index",
            "total_score",
            "wrong_count",
            "wrong_limit",
            "advice_summary",
        ]
        read_only_fields = ["id", "user", "started_at", "last_activity_at"]
