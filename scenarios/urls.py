from django.urls import path
from .views import topics, scenario_detail

urlpatterns = [
    path("topics/", topics, name="topics"),
    path("scenario/<str:topic>/", scenario_detail, name="scenario_detail"),
]
