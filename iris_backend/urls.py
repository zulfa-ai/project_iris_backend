from django.contrib import admin
from django.urls import path, include
from .auth_views import login_view, refresh_view

urlpatterns = [
    path("admin/", admin.site.urls),

    # Cookie-based auth
    path("api/auth/login/", login_view),
    path("api/auth/refresh/", refresh_view),

    path("api/gameplay/", include("gameplay.urls")),
    path("api/", include("scenarios.urls")),
]
