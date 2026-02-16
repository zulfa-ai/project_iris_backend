class GameplayError(Exception):
    status_code = 400
    default_detail = "Gameplay error"

    def __init__(self, detail=None):
        self.detail = detail or self.default_detail


class NotFound(GameplayError):
    status_code = 404
    default_detail = "Not found"


class Forbidden(GameplayError):
    status_code = 403
    default_detail = "Forbidden"


class Conflict(GameplayError):
    status_code = 409
    default_detail = "Conflict"
