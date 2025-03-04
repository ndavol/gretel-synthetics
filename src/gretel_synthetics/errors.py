"""
Custom error classes
"""


class GenerationError(Exception):
    pass


class TooManyInvalidError(RuntimeError):
    pass


class TooFewRecordsError(RuntimeError):
    pass


class InvalidSeedError(RuntimeError):
    pass
