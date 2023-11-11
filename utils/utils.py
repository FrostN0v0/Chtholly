from pathlib import Path
import base64


async def path2base64(path: Path):
    with open(path, "rb") as f:
        byte_data = f.read()
    base64_str = f'base64://{base64.b64encode(byte_data).decode("ascii")}'  # 二进制转base64
    return base64_str
