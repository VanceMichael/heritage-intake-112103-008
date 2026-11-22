"""Errors raised by the intake workbench."""


class IntakeError(ValueError):
    """Base class for workbench input errors."""


class IntakeFormatError(IntakeError):
    """The incoming document is not a supported intake envelope."""
