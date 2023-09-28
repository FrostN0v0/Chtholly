from kirami.utils.utils import new_dir
from kirami.utils.downloader import Downloader
from kirami.config.path import RES_DIR
from pathlib import Path
from kirami.log import logger

base_url = "https://pan.yropo.top/source/mockingbird/"
res_url = "https://pan.yropo.top/home/source/mockingbird/"


async def download_resource(root: Path, model_name: str):
    for file_name in ["g_hifigan.pt", "encoder.pt"]:
        if not (RES_DIR / "mockingbird" / file_name).exists():
            logger.info(f"{file_name}不存在，开始下载{file_name}...请不要退出...")
            res = await Downloader.download_file(url=base_url + file_name, path=root / file_name)
            if not res:
                return False
    for file_name in ["record.wav", f"{model_name}.pt"]:
        if not (root / model_name / file_name).exists():
            logger.info(f"{file_name}不存在，开始下载{file_name}...请不要退出...")
            if file_name == "record.wav":
                url = res_url + model_name + "/record.wav"
            else:
                url = res_url + model_name + f"/{model_name}.pt"
            res = await Downloader.download_file(url, root / model_name / file_name)
            if not res:
                return False
    return True


# 检查资源是否存在
async def check_resource(root: Path, model_name: str):
    for file_name in ["g_hifigan.pt", "encoder.pt"]:
        if not (root / file_name).exists():
            return False
    for file_name in ["record.wav", f"{model_name}.pt"]:
        if not (root / model_name / file_name).exists():
            return False
    return True


# 检查资源目录是否存在
async def check_dir(root: Path, model_name: str):
    if not root.exists():
        new_dir(root)
    if not (root / model_name).exists():
        new_dir(root / model_name)
