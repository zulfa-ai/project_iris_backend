from .models import GameSession
from .exceptions import NotFound, Forbidden


def get_session_for_user(session_id: int, user) -> GameSession:
    session = GameSession.objects.filter(id=session_id).first()
    if not session:
        raise NotFound("session not found")
    if session.user_id != user.id:
        raise Forbidden("not your session")
    return session
