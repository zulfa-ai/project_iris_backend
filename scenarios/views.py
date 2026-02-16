import os
import json
from django.conf import settings
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.decorators import api_view, permission_classes


DATA_FOLDER = os.path.join(settings.BASE_DIR, "scenarios", "data")


@api_view(["GET"])
@permission_classes([AllowAny])
def topics(request):
    files = [
        f.replace(".json", "")
        for f in os.listdir(DATA_FOLDER)
        if f.endswith(".json")
    ]
    return Response({"topics": files})


@api_view(["GET"])
@permission_classes([AllowAny])
def scenario_detail(request, topic):
    file_path = os.path.join(DATA_FOLDER, f"{topic}.json")

    if not os.path.exists(file_path):
        return Response({"error": "Scenario not found"}, status=404)

    with open(file_path, "r") as f:
        data = json.load(f)

    return Response(data)