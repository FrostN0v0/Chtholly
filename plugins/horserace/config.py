from kirami.config import BaseConfig


class Config(BaseConfig):

    send_forward_msg: bool = False
    """是否将赛马过程图片以合并消息发送"""
    pic_font_size: int = 16
    """图片字体大小"""

    """赛马基础配置"""
    # 跑道长度
    setting_track_length: int = 20
    # 随机位置事件，最小能到的跑道距离
    setting_random_min_length: int = 0
    # 随机位置事件，最大能到的跑道距离
    setting_random_max_length: int = 15
    # 每回合基础移动力最小值
    base_move_min: int = 1
    # 每回合基础移动力最大值
    base_move_max: int = 3
    # 最大支持玩家数
    max_player: int = 8
    # 最少玩家数
    min_player: int = 2
    # 超时允许重置最少时间，秒
    setting_over_time: int = 120
    # 事件概率 = event_rate / 1000
    event_rate: int = 450
