"""Configuration for bounded public-channel perception."""

from arclet.entari import BasicConfModel


class ChannelPerceptionConfig(BasicConfModel):
    retention_days: int = 7
    """Maximum age of retained ambient messages."""
    max_messages_per_channel: int = 500
    """Maximum retained ambient messages per account-scoped channel."""
    participant_retention_days: int = 90
    """Maximum age of inactive participant metadata."""
    max_participants_per_channel: int = 1000
    """Maximum retained participant records per account-scoped channel."""
    max_content_chars: int = 2000
    """Maximum normalized characters stored for one message."""
    queue_size: int = 1024
    """Maximum pending observations before new observations are dropped."""
