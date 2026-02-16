from django.db import models


class ScenarioTemplate(models.Model):
    topic = models.CharField(max_length=50)  # "ransomware", "phishing", etc.
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.topic}: {self.name}"


class QuestionTemplate(models.Model):
    scenario = models.ForeignKey(ScenarioTemplate, on_delete=models.CASCADE, related_name="questions")

    stage = models.CharField(max_length=30)        # "prepare/detect/..."
    question_key = models.CharField(max_length=80) # "prep-1"
    prompt = models.TextField()
    order = models.PositiveIntegerField(default=0)

    time_limit_seconds = models.PositiveIntegerField(default=30)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["scenario", "question_key"], name="unique_question_key_per_scenario"),
            models.UniqueConstraint(fields=["scenario", "stage", "order"], name="unique_stage_order_per_scenario"),
        ]

    def __str__(self):
        return f"{self.scenario_id} {self.question_key}"


class ChoiceTemplate(models.Model):
    question = models.ForeignKey(QuestionTemplate, on_delete=models.CASCADE, related_name="choices")

    choice_id = models.CharField(max_length=80)  # "yes/no/maybe" or "A/B/C"
    label = models.CharField(max_length=200)

    points = models.IntegerField(default=0)
    is_correct = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.question_id} {self.choice_id}"