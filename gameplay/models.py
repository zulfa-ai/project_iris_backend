from django.conf import settings
from django.db import models
from django.utils import timezone


class GameSession(models.Model):
    STATUS_CHOICES = [
        ("created", "Created"),
        ("in_progress", "In Progress"),
        ("completed", "Completed"),
        ("failed", "Failed"),
        ("abandoned", "Abandoned"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="game_sessions",
    )

    # If you want to keep this simple for now, keep "topic".
    topic = models.CharField(max_length=50)

    current_stage_index = models.PositiveIntegerField(default=0)
    current_question_index = models.PositiveIntegerField(default=0)

    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="created")
    ended_reason = models.CharField(max_length=50, null=True, blank=True)
    last_activity_at = models.DateTimeField(auto_now=True)

    total_score = models.IntegerField(default=0)

    wrong_count = models.IntegerField(default=0)
    wrong_limit = models.IntegerField(default=5)

    advice_summary = models.TextField(blank=True, default="")

    # Future-proof: store hidden effects / setup decisions / AI seed, etc.
    factors = models.JSONField(default=dict, blank=True)

    def end(self, status: str, reason: str | None = None):
        self.status = status
        self.ended_reason = reason
        self.ended_at = timezone.now()
        self.save(update_fields=["status", "ended_reason", "ended_at"])

    def __str__(self):
        return f"{self.user} - {self.topic} - {self.status}"


class StageRun(models.Model):
    STAGES = [
        ("prepare", "Prepare"),
        ("detect", "Detect"),
        ("analyse", "Analyse"),
        ("remediate", "Remediate"),
        ("post_incident", "Post-Incident"),
    ]
    STATUS = [("locked", "Locked"), ("active", "Active"), ("done", "Done")]

    session = models.ForeignKey(GameSession, on_delete=models.CASCADE, related_name="stages")
    stage = models.CharField(max_length=30, choices=STAGES)
    order = models.PositiveIntegerField()  # 0..4
    status = models.CharField(max_length=10, choices=STATUS, default="locked")

    stage_score = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["session", "stage"], name="unique_stage_per_session"),
            models.UniqueConstraint(fields=["session", "order"], name="unique_stage_order_per_session"),
        ]

    def __str__(self):
        return f"{self.session.id} {self.stage} ({self.status})"


class QuestionRun(models.Model):
    STATUS = [("pending", "Pending"), ("answered", "Answered"), ("skipped", "Skipped")]

    stage_run = models.ForeignKey(StageRun, on_delete=models.CASCADE, related_name="questions")

    # Stable IDs that can refer to template questions now, AI later (optional)
    question_key = models.CharField(max_length=80)  # e.g. "prep-1" or "ai-uuid-123"

    # Snapshot of what the user saw (VERY important for AI & audit)
    prompt = models.TextField()
    choices = models.JSONField(default=list)  # [{id,label,is_correct?,points?}, ...]

    order = models.PositiveIntegerField()  # 0..N in that stage
    status = models.CharField(max_length=10, choices=STATUS, default="pending")

    time_limit_seconds = models.PositiveIntegerField(default=30, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["stage_run", "order"], name="unique_question_order_per_stage_run"),
            models.UniqueConstraint(fields=["stage_run", "question_key"], name="unique_question_key_per_stage_run"),
        ]

    def __str__(self):
        return f"{self.stage_run.id} {self.question_key} ({self.status})"


class Answer(models.Model):
    session = models.ForeignKey(GameSession, on_delete=models.CASCADE, related_name="answers")
    question_run = models.OneToOneField(QuestionRun, on_delete=models.CASCADE, related_name="answer")

    # Store stable choice id instead of text (text can change)
    selected_choice_id = models.CharField(max_length=80)
    selected_text = models.CharField(max_length=200, blank=True, default="")

    score_delta = models.IntegerField(default=0)
    is_correct = models.BooleanField(default=False)

    # Idempotency for double-click / retries (highly recommended)
    client_answer_id = models.UUIDField(null=True, blank=True, unique=True)

    answered_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.session.id} {self.question_run.question_key} {self.score_delta}"