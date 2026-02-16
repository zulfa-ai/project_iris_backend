from django.urls import path
from . import views

urlpatterns = [
    path("health/", views.health, name="health"),
    path("session/start/", views.start_or_resume, name="start_or_resume"),
    path("session/<int:session_id>/current/", views.current_state, name="current_state"),
    path("session/<int:session_id>/answer/", views.submit_answer, name="submit_answer"),
    path("session/<int:session_id>/quit/", views.quit_session, name="quit_session"),
    path("sessions/history/", views.history, name="history"),
]
