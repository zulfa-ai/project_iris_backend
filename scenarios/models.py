from django.db import models


# =========================================================
# SCENARIO TEMPLATES
# =========================================================

class ScenarioTemplate(models.Model):
    topic = models.CharField(max_length=50)  # phishing / ransomware / malware
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.topic}: {self.name}"


# =========================================================
# QUESTION TEMPLATES
# =========================================================

class QuestionTemplate(models.Model):

    scenario = models.ForeignKey(
        ScenarioTemplate,
        on_delete=models.CASCADE,
        related_name="questions"
    )

    # Stage of incident lifecycle
    stage = models.CharField(
        max_length=30
    )  # prepare / detect / analyse / remediation / post-incident

    question_key = models.CharField(
        max_length=80
    )  # prep-1, detect-2 etc

    prompt = models.TextField()

    order = models.PositiveIntegerField(default=0)

    time_limit_seconds = models.PositiveIntegerField(default=30)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["scenario", "question_key"],
                name="unique_question_key_per_scenario"
            ),
            models.UniqueConstraint(
                fields=["scenario", "stage", "order"],
                name="unique_stage_order_per_scenario"
            ),
        ]

    def __str__(self):
        return f"{self.scenario.topic} - {self.question_key}"


# =========================================================
# ANSWER OPTIONS
# =========================================================

class ChoiceTemplate(models.Model):

    question = models.ForeignKey(
        QuestionTemplate,
        on_delete=models.CASCADE,
        related_name="choices"
    )

    choice_id = models.CharField(max_length=80)  # A / B / C

    label = models.CharField(max_length=200)

    # scoring system
    # +10 = correct
    # -5  = unsure
    # -10 = wrong
    points = models.IntegerField(default=0)

    is_correct = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.question_id} {self.choice_id}"


# =========================================================
# GAME SESSION (SIMULATION RUN)
# =========================================================

class GameSession(models.Model):

    user = models.ForeignKey(
        "auth.User",
        on_delete=models.CASCADE,
        related_name="scenario_sessions"
    )

    topic = models.CharField(max_length=50)

    difficulty = models.CharField(max_length=20)

    started_at = models.DateTimeField(auto_now_add=True)

    ended_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=20,
        default="in_progress"
    )

    ended_reason = models.CharField(
        max_length=100,
        blank=True,
        null=True
    )

    # =================================================
    # SCORE SYSTEM
    # =================================================

    total_score = models.IntegerField(default=0)

    wrong_count = models.IntegerField(default=0)

    wrong_limit = models.IntegerField(default=5)

    # =================================================
    # INCIDENT PRESSURE SYSTEM
    # =================================================

    pressure_level = models.IntegerField(default=0)

    # 0–19 = normal
    # 20–39 = elevated
    # 40–69 = high
    # 70–100 = critical

    def get_severity(self):

        if self.pressure_level >= 70:
            return "critical"

        if self.pressure_level >= 40:
            return "high"

        if self.pressure_level >= 20:
            return "elevated"

        return "normal"

    def __str__(self):
        return f"Session {self.id} ({self.topic})"