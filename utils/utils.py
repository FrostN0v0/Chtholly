import base64
from pathlib import Path


async def path2base64(path: Path) -> str:
    with open(path, "rb") as f:
        byte_data = f.read()
    base64_str = (
        f'base64://{base64.b64encode(byte_data).decode("ascii")}'  # 二进制转base64
    )
    return base64_str


def new_dir(path: str | Path, root: str | Path = Path.cwd()) -> Path:
    """创建一个新的目录。

    ### 参数
        path: 新目录的路径

        root: 相对于新目录的根目录。默认为 bot 根目录

    ### 返回
        新目录的绝对路径
    """
    root = Path(root)

    if root.is_file():
        raise RuntimeError("root 应该是一个目录, 而不是一个文件")

    dir_ = root / path
    dir_.mkdir(parents=True, exist_ok=True)

    return dir_.resolve()
