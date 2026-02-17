from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import authenticate
from django.conf import settings


@api_view(["POST"])
@permission_classes([AllowAny])
def login_view(request):
    """
    POST:
    {
        "username": "...",
        "password": "..."
    }
    """

    username = request.data.get("username")
    password = request.data.get("password")

    user = authenticate(username=username, password=password)

    if user is None:
        return Response({"detail": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)

    refresh = RefreshToken.for_user(user)

    response = Response({
        "access": str(refresh.access_token),
    })

    # Set refresh token in HttpOnly cookie
    response.set_cookie(
        key="refresh_token",
        value=str(refresh),
        httponly=True,
        secure=False,  # True in production (HTTPS)
        samesite="Lax",
        max_age=90 * 24 * 60 * 60,  # 90 days
    )

    return response


@api_view(["POST"])
@permission_classes([AllowAny])
def refresh_view(request):
    refresh_token = request.COOKIES.get("refresh_token")

    if not refresh_token:
        return Response({"detail": "No refresh token"}, status=status.HTTP_401_UNAUTHORIZED)

    try:
        refresh = RefreshToken(refresh_token)
        access_token = str(refresh.access_token)

        return Response({"access": access_token})

    except Exception:
        return Response({"detail": "Invalid refresh token"}, status=status.HTTP_401_UNAUTHORIZED)