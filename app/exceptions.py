class SchedulerError(Exception):
    """Base class for all domain errors."""


class NotFoundError(SchedulerError):
    pass


class SlotUnavailableError(SchedulerError):
    """Raised when a slot cannot be held/booked because it's no longer
    AVAILABLE (already held/booked by someone else, or a stale request)."""


class HoldExpiredError(SchedulerError):
    pass


class HoldOwnershipError(SchedulerError):
    """Raised when a patient tries to confirm/release a hold they don't own."""


class InvalidWindowError(SchedulerError):
    pass
