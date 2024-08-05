from .new_Catalog import *
from .upload_img import *
from .delete_img import *
from .send_img import *
from kirami.server import Server
from fastapi import APIRouter
from fastapi.responses import FileResponse

api: APIRouter = Server.get_router("/gallery", tags=["Gallery"])


@api.get("/random")
async def get_gallery(path: str):
    img = Resource.image(gallery_path / path).choice()
    return FileResponse(img.path)
