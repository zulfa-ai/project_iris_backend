from django.urls import path
from gameplay import views

urlpatterns = [

    # Health check
    path("health/", views.health, name="health"),

    # Start new hybrid AI session
    path("session/start/", views.session_start, name="session_start"),

    # Session gameplay
    path("session/<int:session_id>/current/", views.current_state, name="current_state"),
    path("session/<int:session_id>/answer/", views.submit_answer, name="submit_answer"),
    path("session/<int:session_id>/quit/", views.quit_session, name="quit_session"),

    # History
    path("sessions/history/", views.history, name="history"),

]