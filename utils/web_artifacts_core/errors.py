"""Public exception hierarchy for the artifact store."""

from __future__ import annotations


class ArtifactError(ValueError):
    """Base class for malformed, unavailable, or otherwise invalid artifacts."""


class ArtifactNotFound(ArtifactError):
    """The requested artifact, file, capability, or derivative does not exist."""


class ArtifactAccessDenied(ArtifactError):
    """The artifact exists but is outside the caller's ownership scope."""


class ArtifactLimitError(ArtifactError):
    """A configured file, project, active-count, or byte quota was exceeded."""
