"""Configuration for isolated acceptance and reviewed native plugin hosting."""

from arclet.entari import BasicConfModel


class WorkshopConfig(BasicConfModel):
    sandbox_image: str = "chtholly-workshop:local"
    """Prebuilt trusted Docker image; submissions never build or pull images."""
    sandbox_timeout_seconds: float = 180.0
    """Maximum isolated acceptance duration in seconds."""
    sandbox_memory_mb: int = 1024
    """Container memory limit in MiB."""
    sandbox_cpus: float = 1.0
    """Container CPU quota."""
    max_total_source_bytes: int = 64 * 1024 * 1024
    """Total immutable source storage quota."""
    max_versions_per_plugin: int = 256
    """Maximum immutable versions retained per generated plugin."""
