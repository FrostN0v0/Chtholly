from kirami.server import Server
from kirami.config.path import IMAGE_DIR
from fastapi import APIRouter, Request, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from kirami.utils import Resource
from fastapi.templating import Jinja2Templates
from .config import template_dir, static_dir, config
import math

api: APIRouter = Server.get_router("/gallery", tags=["gallery"])

app: FastAPI = Server.get_app()


gallery_path = IMAGE_DIR / 'gallery'

templates = Jinja2Templates(directory=str(template_dir))

app.mount("/gallery/static", StaticFiles(directory=str(static_dir)), name="test")


@api.get("/get")
async def get_gallery(path: str):
    img = Resource.image(gallery_path / path).choice()
    return FileResponse(img.path)


@api.get("/byid")
async def get_gallery_byid(path: str, pic_id: str):
    img = Resource.image(gallery_path / path / f"{pic_id}.png")
    return FileResponse(img.path)


@api.get("/path")
async def get_gallery_path():
    dir_list = []
    for i in gallery_path.iterdir():
        dir_list.append(i.name)
    return dir_list


@api.get("/list")
async def get_gallery_show(request: Request, path: str):
    img_list = []
    img_per_page = 12
    for i in (gallery_path / path).iterdir():
        img_list.append(i)
    img_list = sorted(img_list, key=lambda x: x.stat().st_ctime)
    total_pages = math.ceil(len(img_list) / img_per_page)
    user_agent = request.headers.get("user-agent")
    if is_mobile(user_agent):
        background_image = "https://www.loliapi.com/acg/pe/"
    else:
        background_image = "https://www.loliapi.com/acg/pc/"
    return templates.TemplateResponse("index.html", {
        "request": request,
        "path": path,
        "total_pages": total_pages,
        "background_image": background_image,
        "icp_id": config.icp_id
    })


@api.get("/images")
async def get_gallery_images(request: Request, path: str, page: int = 1):
    img_list = []
    img_per_page = 12
    for i in (gallery_path / path).iterdir():
        img_list.append(i)
    img_list = sorted(img_list, key=lambda x: x.stat().st_ctime)
    start_img = (page - 1) * img_per_page + 1
    end_img = start_img + img_per_page
    image_ids = range(start_img, min(end_img, len(img_list) + 1))

    context = {
        "request": request,
        "image_ids": image_ids,
        "path": path,
    }
    return templates.TemplateResponse("images.html", context)

# todo：图库跳转列表界面
# @api.get("/galleries")
# async def get_gallery_list(request: Request, path: str):


def is_mobile(user_agent: str) -> bool:
    """判断 User-Agent 是否表示移动设备"""
    mobile_user_agents = [
        "Mobile", "Android", "iPhone", "iPad", "iPod", "Windows Phone",
        "IEMobile", "Opera Mini", "BlackBerry", "webOS", "Silk"
    ]
    return any(mobile_user_agent in user_agent for mobile_user_agent in mobile_user_agents)
