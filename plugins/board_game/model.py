from datetime import datetime
from mango import Document, Field


class GameRecord(Document):
    game_id: str
    session_id: str
    name: str
    start_time: datetime = datetime.now()
    update_time: datetime = datetime.now()
    player_black_id: str = Field(max_length=64, default="")
    player_black_name: str = Field(default="")
    player_white_id: str = Field(max_length=64, default="")
    player_white_name: str = Field(default="")
    positions: str = Field(default="")
    is_game_over: bool = Field(default=False)

