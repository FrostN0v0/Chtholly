from .config import Config, var
from asyncio import Future, sleep, Queue, create_task
from ujson import dumps, loads
from uuid import uuid4
from nonebot import get_driver
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO

driver = get_driver()

config = Config.load_config()


@driver.on_startup
async def _():
    """处理队列协程"""
    task = create_task(req_queue(), name="req_queue")
    var.background_tasks.add(task)
    task.add_done_callback(var.background_tasks.discard)


async def req_queue():
    """请求队列"""
    var.queue = Queue()
    while True:
        # 获取队列元素
        q = await var.queue.get()
        result = await handle_req(q["id"], q["req_text"], q["operation"])
        # 保存结果
        q["result"] = result
        q["future"].set_result(result)
        # 标记完成
        var.queue.task_done()


def body(message: str, conversation_id: str, parent_message_id: str):
    """构造请求参数"""
    req_body = {
        "action": "next",
        "messages": [
            {
                "id": str(uuid4()),
                "author": {"role": "user"},
                "role": "user",
                "content": {"content_type": "text", "parts": [message]},
            }
        ],
        "parent_message_id": parent_message_id,
        "model": config.talk_with_chatgpt_api_model,
    }
    if conversation_id:
        req_body["conversation_id"] = conversation_id

    return dumps(req_body)


async def handle_req(id: str, req_text: str, operation: str) -> str:
    """请求chatgpt"""
    resp_code = 0
    err_msg = ""
    new_talk = False
    if not var.session_data[id][1]:
        new_talk = True
        var.session_data[id][1] = str(uuid4())
    # 如果是新会话且不是预设请求且预设里面有内容
    if new_talk and operation != "prompt":
        if prompt_text := var.prompt_list[var.session_data[id][2]]:
            await handle_req(id, prompt_text, "prompt")
    if operation == "talk" or operation == "prompt":
        url = config.talk_with_chatgpt_api_addr
        content = body(req_text, var.session_data[id][0], var.session_data[id][1])
    elif operation == "rename":
        url = f"{config.talk_with_chatgpt_api_addr}/{var.session_data[id][0]}"
        content = dumps({"title": id})
    else:
        url = f"{config.talk_with_chatgpt_api_addr}/{var.session_data[id][0]}"
        content = dumps({"is_visible": False})

    try:
        # 发起请求
        resp = await var.httpx_client.post(
            url,
            content=content,
        )
        # 非对话请求响应结果无所谓，发出去就行
        if operation == "rename" or operation == "delete":
            return ""
        # 响应码
        resp_code = resp.status_code
        # 先存着结果，如果报错了用于判断
        err_msg = resp.text
        # 提取回答
        pre_data = resp.text.split("\n\n")
        json_data = loads(pre_data[pre_data.index("data: [DONE]") - 2][6:])
        # 保存新的会话id
        # if not var.session_data[id][0]:
        var.session_data[id][0] = json_data["conversation_id"]
        var.session_data[id][1] = json_data["message"]["id"]

        # 尝试重命名（需api支持）
        if new_talk:
            await handle_req(id, "", "rename")
        result: str = json_data["message"]["content"]["parts"][0]

    except Exception as e:
        # 会话丢失
        if resp_code == 404 and "found" in err_msg:
            # 清空会话id
            var.session_data[id][0] = ""
            var.session_data[id][1] = ""
            return await handle_req(id, req_text, operation)
        # 冲突，等待2秒再尝试
        if "Only one message at a time" in err_msg:
            await sleep(2)
            return await handle_req(id, req_text, operation)
        # 真的出错了
        err_header = "响应结果异常！" if resp_code else "访问接口出错！"
        code_text = f"\n响应码: {resp_code}" if resp_code else ""
        err_type_text = "" if resp_code else f"\n错误类型: {repr(e)}"
        content_text = f"\n响应内容: {err_msg}" if resp_code else ""
        content_text = (
            f"{content_text[:300]}\n（中间内容过多，忽略）\n{content_text[-300:]}"
            if len(content_text) > 300
            else content_text
        )
        result = err_header + code_text + err_type_text + content_text

    return result


async def req_chatgpt(id: str, req_text: str, operation: str = "talk") -> str:
    """把请求塞入队列并等结果返回"""
    # typing报错所以加上
    if not var.queue:
        return ""
    if var.enable is False:
        return "插件未正确配置，无法请求！"
    # 把事件塞入队列，等待结果返回
    future = Future()
    item = {"id": id, "req_text": req_text, "operation": operation, "future": future}
    await var.queue.put(item)
    # 返回结果
    resp_text: str = await future
    # 如果是开发者模式只取需要的回答
    if var.session_data[id][2] == "开发者模式":
        resp_text = resp_text.replace("(Normal Output) ", "")
        if "(Developer Mode Output) " in resp_text:
            resp_text = resp_text.split("(Developer Mode Output) ")[1]

    return resp_text


# 文字转图片
def text_to_img(text: str, font_path: str = config.talk_with_chatgpt_font_path) -> BytesIO:
    """
    字转图片
    """
    lines = text.splitlines()
    line_count = len(lines)
    # 读取字体
    font = ImageFont.truetype(font_path, config.talk_with_chatgpt_font_size)
    # 获取字体的行高
    left, top, width, line_height = font.getbbox("a")
    # 增加行距
    line_height += 3
    # 获取画布需要的高度
    height = line_height * line_count + 20
    # 获取画布需要的宽度
    width = int(max([font.getlength(line) for line in lines])) + 25
    # 字体颜色
    black_color = (0, 0, 0)
    # 生成画布
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    # 按行开画，c是计算写到第几行
    c = 0
    for line in lines:
        draw.text((10, 6 + line_height * c), line, font=font, fill=black_color)
        c += 1
    img_bytes = BytesIO()
    image.save(img_bytes, format="jpeg")
    return img_bytes
