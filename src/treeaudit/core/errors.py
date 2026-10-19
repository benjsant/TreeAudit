"""Typed errors raised by TreeAudit."""


class TreeAuditError(Exception):
    """Base class for all errors raised on purpose by TreeAudit."""


class ConfigError(TreeAuditError):
    """The configuration file is missing, unreadable or invalid."""
