"""Interactive group chat plugin.

Galgame-style multi-axis relationship engine (LLM-evaluated), vision image
tagging, context-aware media sending, optional TTS voice replies and plugin
function calling. Hard deps: llm + database plugins. TTS is optional.
"""

import base64
import random
import asyncio
from datetime import datetime
from collections import deque

from launart import Launart
import litellm
from arclet.entari import (
    Audio,
    Image,
    Session,
    MessageChain,
    MessageCreatedEvent,
    plugin,
    command,
    filter_,
    metadata,
    scheduler,
    plugin_config,
)
from arclet.letoderea import BLOCK
from entari_plugin_llm import LLMToolEvent, llm  # entari: plugin
from arclet.entari.filter import superusers
from arclet.entari.logger import log
from arclet.entari.plugin import PluginRole
from entari_plugin_database import select, get_session  # entari: plugin
from arclet.letoderea.context import Contexts
from entari_plugin_llm.config import get_model_config

from utils.path import AUDIO_DIR, IMAGE_DIR

from .media import (
    match_audio,
    match_image,
    parse_audio_text,
    format_image_note,
    is_random_request,
    normalize_image_tags,
    normalize_image_description,
)
from .tools import tts_temp_path, truncate_for_tts
from .config import LLMChatConfig
from .models import ImageTag
from .persona.store import (
    get_mood,
    set_mood,
    get_relation,
    load_history,
    nightly_decay,
    save_relation,
    append_message,
)
from .persona.runner import run_evaluation
from .persona.compose import energy_at, compose_persona_prompt
from .persona.profile import decode_embedding, encode_embedding, cosine_similarity
from .persona.memory_store import embed_text, load_memory_context, apply_memory_updates

metadata(
    name="llm_chat",
    author=[{"name": "FrostN0v0"}],
    version="0.1.0",
    description="群聊会话互动：多轴关系引擎 + 媒体互动 + 可选语音回复",
    role=PluginRole.NORMAL,
)

config = plugin_config(LLMChatConfig)
plug = plugin.Plugin.current()

HELP_META = {"icon": "\U0001f4ac", "category": "互动"}

_LOGGER = log.wrapper("[llm_chat]")
DINGGONG_DIR = AUDIO_DIR / "dinggong"
registered_tools: list[str] = []

_RECENT_IMAGE_WINDOW = 5
_IMAGE_FETCH_TIMEOUT = 15.0
_VISION_TIMEOUT = 120.0
_IMAGE_FETCH_MAX_BYTES = 6 * 1024 * 1024
_IMAGE_DESC_CACHE_MAX = 128
_image_vectors: dict[str, list[float]] = {}
_recent_images: dict[str, deque[str]] = {}
_image_desc_cache: dict[str, str] = {}


async def _pick_image(rows: list[ImageTag], context: str, recent: deque[str]) -> str | None:
    """Semantic retrieval first, IDF tag matching as fallback; recent picks excluded."""
    paths = [row.file_path for row in rows]
    if is_random_request(context):
        pool = [path for path in paths if path not in recent] or paths
        return random.choice(pool)
    query = await embed_text(config, context)
    if query is not None:
        candidates: list[tuple[str, float]] = []
        for row in rows:
            vector = _image_vectors.get(row.file_path)
            if vector is None:
                vector = decode_embedding(row.embedding_json)
                if vector is None:
                    continue
                _image_vectors[row.file_path] = vector
            score = cosine_similarity(query, vector)
            if score >= config.image_match_min_similarity:
                candidates.append((row.file_path, score))
        candidates.sort(key=lambda item: item[1], reverse=True)
        top = [path for path, _ in candidates[: config.image_top_candidates]]
        pool = [path for path in top if path not in recent] or top
        if pool:
            return random.choice(pool)
    tagged = [(row.file_path, row.tags) for row in rows]
    fallback = [(path, tags) for path, tags in tagged if path not in recent] or tagged
    return match_image(context, fallback)


def _sentence_truncate(text: str, limit: int) -> str:
    return truncate_for_tts(text, limit)


def _setup_tools() -> list[str]:
    tools = plugin.dispatch(LLMToolEvent)
    registered: list[str] = []

    @tools
    async def send_image(session: Session, context: str) -> str:
        """
        Send a local reaction image or sticker matching compact keywords.

        Args:
            context (str): Short emotion/scenario tags, for example "害羞 可爱 早安"; "随便" picks randomly.
        Returns:
            str: Delivery result.
        """
        async with get_session() as db:
            rows = list((await db.execute(select(ImageTag))).scalars().all())
        if not rows:
            return "没有可用的图片"
        recent = _recent_images.setdefault(session.channel.id, deque(maxlen=_RECENT_IMAGE_WINDOW))
        rel_path = await _pick_image(rows, context, recent)
        if rel_path is None:
            return "没有合适的图片"
        full = IMAGE_DIR / rel_path
        if not full.exists():
            return "图片文件已丢失"
        await session.send(MessageChain([Image.of(path=full)]))
        recent.append(rel_path)
        row = next((r for r in rows if r.file_path == rel_path), None)
        tag_hint = "，".join((row.tags if row else context).split("，")[:5])
        await append_message(session.channel.id, "", "bot", "assistant", f"[发送了表情包: {tag_hint}]")
        return f"已发送图片（{context}）"

    registered.append("send_image")

    if DINGGONG_DIR.exists():
        clip_texts = [text for file in sorted(DINGGONG_DIR.glob("*.mp3")) if (text := parse_audio_text(file.name))]
        inventory = "；".join(clip_texts)

        async def send_audio(session: Session, context: str) -> str:
            files = sorted(DINGGONG_DIR.glob("*.mp3"))
            if is_random_request(context):
                pool = [file for file in files if parse_audio_text(file.name)]
                matched = random.choice(pool) if pool else None
            else:
                matched = match_audio(context, files)
            if matched is None:
                return "没有合适的语音片段"
            await session.send(MessageChain([Audio.of(path=matched)]))
            await append_message(
                session.channel.id, "", "bot", "assistant", f"[发送了语音: {parse_audio_text(matched.name)}]"
            )
            return f"已发送语音：{parse_audio_text(matched.name)}"

        send_audio.__doc__ = f"""
        Send a prerecorded local voice clip matching compact keywords.

        Available clip lines: {inventory}

        Args:
            context (str): Tone/scenario keywords or a quote from the clip list; "随便" picks randomly.
        Returns:
            str: Spoken text in the selected clip.
        """
        tools(send_audio)
        registered.append("send_audio")

    if config.tts_enabled:

        @tools
        async def speak(session: Session, text: str) -> str:
            """
            Synthesize a short sentence and send it as voice.

            Args:
                text (str): Short text to speak.
            Returns:
                str: Delivery result.
            """
            speech = _sentence_truncate(text, config.tts_max_chars)
            try:
                service = Launart.current().get_component("tts.service")
                audio = await service.synthesize(speech)  # type: ignore[attr-defined]
            except Exception:
                return "语音服务暂不可用"
            out = tts_temp_path()
            from pathlib import Path

            Path(out).write_bytes(audio)
            await session.send(MessageChain([Audio.of(path=out)]))
            await append_message(session.channel.id, "", "bot", "assistant", f"[用语音说: {speech}]")
            return f"已用语音说出：{speech}"

        registered.append("speak")

    if config.allowed_commands:

        @tools
        async def call_plugin(session: Session, command_line: str) -> str:
            """
            Execute one whitelisted bot command.

            Args:
                command_line (str): Full command line, for example "echo hello".
            Returns:
                str: Command result text.
            """
            head = command_line.split(maxsplit=1)[0] if command_line.strip() else ""
            if head not in config.allowed_commands:
                return f"指令 {head or '(空)'} 不在允许列表中"
            result = await command.execute(command_line, session)
            return str(result) if result is not None else "指令已执行"

        registered.append("call_plugin")

    registered_tools[:] = registered
    return registered


_registered = _setup_tools()
_LOGGER.info(f"registered LLM tools: {', '.join(_registered) or '(none)'}")


_active_tag_pass: asyncio.Task | None = None
_active_tag_scope: str | None = None


def _cancel_active_tag_pass() -> None:
    if _active_tag_pass is not None and not _active_tag_pass.done():
        _active_tag_pass.cancel()


plugin.collect_disposes(_cancel_active_tag_pass)


async def _launch_tag_pass(
    scope: str,
    limit: int | None,
    *,
    retag: bool,
    session: Session | None = None,
) -> str:
    """Start an exclusive tagging pass, cancelling any pass already running.

    Last command wins: the startup incremental pass and manual retag passes
    never run concurrently, so vision calls and DB writes are not duplicated.
    """
    global _active_tag_pass, _active_tag_scope
    cancelled = None
    if _active_tag_pass is not None and not _active_tag_pass.done():
        cancelled = _active_tag_scope
        _active_tag_pass.cancel()
        try:
            await _active_tag_pass
        except asyncio.CancelledError:
            pass
    on_progress = await _progress_reporter(session, scope) if session is not None else None
    _active_tag_scope = scope
    _active_tag_pass = asyncio.create_task(_tag_images(limit, retag=retag, on_progress=on_progress))
    if cancelled:
        return f"已终止运行中的「{cancelled}」任务，开始{scope}，"
    return f"已开始{scope}，"


@plug.use("::startup")
async def _tag_images_on_startup():
    if not config.image_tags_enabled:
        return
    await _launch_tag_pass("启动增量标注", config.tag_batch_size, retag=False)


async def _vision_completion(data_url: str, system_prompt: str, user_text: str) -> str:
    """Vision call bypassing LiteLLM's stale capability gate (shared by tag/describe)."""
    model = get_model_config(config.image_tag_model)
    response = await litellm.acompletion(
        model=model.name,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        base_url=model.base_url,
        api_key=model.api_key,
        timeout=_VISION_TIMEOUT,
        **model.extra,
    )
    return (response.choices[0].message.content or "").strip()  # type: ignore[union-attr]


async def _generate_image_tags(data_url: str) -> str:
    """Generate normalized image tags without LiteLLM's stale vision capability gate."""
    raw = await _vision_completion(data_url, config.image_tag_prompt, "Tag this image for chat reaction retrieval.")
    return normalize_image_tags(raw)


def _raw_to_data_url(data: bytes) -> str | None:
    """bytes -> data URL via satori's built-in mime sniffing; None for non-images."""
    try:
        src = Image.of(raw=data).src
    except ValueError:  # fleep cannot detect the mime type
        return None
    return src if src.startswith("data:image/") else None


async def _fetch_image_data_url(session: Session, src: str) -> str | None:
    """Resolve an element src to a base64 data URL; None when unusable."""
    if src.startswith("data:"):
        return src if src.startswith("data:image/") else None
    if src.startswith("base64://"):  # onebot convention; not a real URL
        return _raw_to_data_url(base64.b64decode(src[9:]))
    data = await asyncio.wait_for(session.download(src), timeout=_IMAGE_FETCH_TIMEOUT)
    if len(data) > _IMAGE_FETCH_MAX_BYTES:
        return None
    return _raw_to_data_url(data)


async def _describe_image(session: Session, src: str) -> str:
    """Describe one inbound image; '' means degrade to a bare placeholder."""
    # Full src as key: NT multimedia URLs carry the file id in the QUERY
    # (path is a constant /download), so stripping it collapses distinct images.
    # Rotated rkey only costs a cache miss, never a wrong hit.
    cache_key = src
    cached = _image_desc_cache.get(cache_key)
    if cached is not None:
        return cached
    data_url = await _fetch_image_data_url(session, src)
    if data_url is None:
        return ""
    raw = await _vision_completion(
        data_url, config.image_describe_prompt, "Describe this chat image for conversation context."
    )
    description = normalize_image_description(raw)
    if description:
        if len(_image_desc_cache) >= _IMAGE_DESC_CACHE_MAX:
            _image_desc_cache.pop(next(iter(_image_desc_cache)))
        _image_desc_cache[cache_key] = description
    return description


async def _tag_images(
    limit: int | None = None,
    *,
    retag: bool = False,
    on_progress=None,
) -> tuple[int, int, int]:
    """Tag local images with vision keywords and return tagged, failed, remaining.

    Images are processed with bounded concurrency (config.tag_concurrency).
    ``on_progress``: optional async callable(tagged, failed, total) called at
    start, every 50 completions, and at end.
    """
    counter = {"tagged": 0, "failed": 0}
    remaining = 0
    try:
        async with get_session() as db:
            known = {row.file_path for row in (await db.execute(select(ImageTag))).scalars().all()}
        candidates = [
            p
            for p in sorted(IMAGE_DIR.rglob("*"))
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
            and (retag or str(p.relative_to(IMAGE_DIR)) not in known)
        ]
        batch = candidates if limit is None else candidates[: max(0, limit)]
        remaining = max(0, len(candidates) - len(batch))
        total = len(batch)
        if not batch:
            if on_progress is not None:
                await on_progress(0, 0, 0)
            return counter["tagged"], counter["failed"], remaining
        scope = "retagging" if retag else "tagging"
        _LOGGER.info(f"{scope} {total} images ({remaining} remain)")
        if on_progress is not None:
            await on_progress(0, 0, total)
        semaphore = asyncio.Semaphore(max(1, config.tag_concurrency))

        async def _tag_one(path) -> None:
            rel_path = str(path.relative_to(IMAGE_DIR))
            async with semaphore:
                try:
                    data = base64.b64encode(path.read_bytes()).decode()
                    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
                    tags = await _generate_image_tags(f"data:{mime};base64,{data}")
                    if not tags:
                        counter["failed"] += 1
                        return
                    vector = await embed_text(config, tags)
                    embedding_json = encode_embedding(vector) if vector is not None else ""
                    async with get_session() as db:
                        existing = (
                            await db.execute(select(ImageTag).where(ImageTag.file_path == rel_path))
                        ).scalar_one_or_none()
                        if existing is None:
                            db.add(ImageTag(file_path=rel_path, tags=tags, embedding_json=embedding_json))
                        else:
                            existing.tags = tags
                            if embedding_json:
                                existing.embedding_json = embedding_json
                        await db.commit()
                    _image_vectors.pop(rel_path, None)
                    counter["tagged"] += 1
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    counter["failed"] += 1
                    _LOGGER.warning(f"tagging failed for {path.name}: {e!r}")
            done = counter["tagged"] + counter["failed"]
            if on_progress is not None and done % 50 == 0 and done < total:
                await on_progress(counter["tagged"], counter["failed"], total)

        await asyncio.gather(*(_tag_one(path) for path in batch))
        if on_progress is not None:
            await on_progress(counter["tagged"], counter["failed"], total)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        # e.g. schema migration not finished yet on first startup after a model change
        _LOGGER.warning(f"image tagging pass aborted: {e!r}")
    return counter["tagged"], counter["failed"], remaining


async def _progress_reporter(session: Session, scope: str):
    """Return an async callback that sends tagging progress to the session."""

    async def report(tagged: int, failed: int, total: int) -> None:
        done = tagged + failed
        if total == 0:
            await session.send(f"{scope}：没有需要处理的图片。")
        elif done == 0:
            concurrency = max(1, config.tag_concurrency)
            est_min = max(1, round(total * 3 / concurrency / 60))
            await session.send(f"{scope}：共 {total} 张，并发 {concurrency}，预计约 {est_min} 分钟。")
        elif done >= total:
            await session.send(f"{scope}完成：成功 {tagged}，失败 {failed}。")
        else:
            await session.send(f"{scope}进度：{done}/{total}（失败 {failed}）")

    return report


@command.on("llmchat retag-images")
@superusers()
async def retag_images(session: Session):
    """Retag up to 50 local chat images with the configured vision model."""
    status = await _launch_tag_pass("重标 50 张", 50, retag=True, session=session)
    await session.send(status + "进度稍后报告。")


@command.on("llmchat tag-images")
@superusers()
async def tag_images(session: Session):
    """Tag every remaining untagged chat image with the configured vision model."""
    status = await _launch_tag_pass("增量标注", None, retag=False, session=session)
    await session.send(status + "进度稍后报告。")


@command.on("llmchat retag-images-all")
@superusers()
async def retag_images_all(session: Session):
    """Retag all local chat images with the configured vision model."""
    status = await _launch_tag_pass("全量重标", None, retag=True, session=session)
    await session.send(status + "进度稍后报告。")


@scheduler.cron("0 4 * * *")
async def _nightly_decay():
    await nightly_decay()
    _LOGGER.info("nightly relationship decay applied")


@plug.dispatch(MessageCreatedEvent).register(priority=900)
@filter_.to_me
async def on_chat(session: Session, ctx: Contexts):
    content = session.elements.extract_plain_text().strip()

    image_notes: list[str] = []
    if config.image_understanding_enabled:
        direct = list(session.elements.select(Image))
        quote = session.quote
        quoted = list(MessageChain(quote.children).select(Image)) if quote and quote.children else []
        ordered = [(img, False) for img in direct] + [(img, True) for img in quoted]
        cap = max(0, config.image_describe_max_per_message)
        described = ordered[:cap]
        overflow = ordered[cap:]

        async def _note(img: Image, is_quoted: bool) -> str:
            try:
                description = await _describe_image(session, img.src)
            except Exception as e:
                _LOGGER.warning(f"image describe failed: {e!r}")
                description = ""
            return format_image_note(description, quoted=is_quoted)

        image_notes = list(await asyncio.gather(*(_note(img, q) for img, q in described)))
        image_notes += [format_image_note("", quoted=q) for _, q in overflow]

    if not content and not image_notes:
        return None
    if image_notes:
        content = " ".join(part for part in [content, *image_notes] if part)

    channel_id = session.channel.id
    user_id = session.user.id
    user_name = (session.member.nick if session.member else None) or session.user.name or user_id

    rel = await get_relation(user_id, channel_id)
    mood = await get_mood(channel_id)
    energy = energy_at(datetime.now().hour)

    memory_context = await load_memory_context(config, user_id, channel_id, content)

    history = await load_history(channel_id, config.context_window)
    messages = [
        {
            "role": row.role if row.role == "assistant" else "user",
            "content": f"[{row.user_name}]: {row.content}" if row.role == "user" else row.content,
        }
        for row in history
    ]
    messages.append({"role": "user", "content": f"[{user_name}]: {content}"})

    system = compose_persona_prompt(
        config.persona,
        mood,
        energy,
        affection=rel.affection,
        trust=rel.trust,
        dependence=rel.dependence,
        resentment=rel.resentment,
        familiarity=rel.familiarity,
        impression=rel.impression,
        profile_facts=memory_context.profile_facts,
        relevant_memories=memory_context.relevant_memories,
        user_name=user_name,
    )

    # Persist the user line before generate: tool calls during generation append
    # media rows that must sort after it; a failed generate still keeps the message.
    await append_message(channel_id, user_id, user_name, "user", content)

    # llm.generate resolves model=None against the "$default" scope only, so
    # resolve against this channel ourselves to follow "/llm model" switches.
    # A stale channel default (e.g. renamed model) falls back to the global default.
    try:
        model_name = get_model_config(config.model, channel_id).name
    except Exception as e:
        _LOGGER.warning(f"channel model resolve failed, using global default: {e!r}")
        model_name = None
    try:
        response = await llm.generate(messages, system=system, model=model_name, ctx=ctx)
    except Exception as e:
        _LOGGER.warning(f"llm generate failed: {e!r}")
        return None

    reply = response.choices[0].message.content or ""  # type: ignore[union-attr]
    if reply and reply != "[END_OF_RESPONSE]":
        await session.send(reply)

    if reply and reply != "[END_OF_RESPONSE]":
        await append_message(channel_id, "", "bot", "assistant", reply)

    # Mechanical familiarity bump; all other axes move only via the evaluator.
    familiarity = min(100.0, rel.familiarity + 1)
    axes = {
        "affection": rel.affection,
        "trust": rel.trust,
        "dependence": rel.dependence,
        "resentment": rel.resentment,
    }
    impression = rel.impression
    counter = rel.eval_counter + 1

    if counter >= config.eval_every_n:
        counter = 0
        recent = history[-config.eval_context_window :] if config.eval_context_window > 0 else []

        def _transcript_line(row) -> str:
            if row.role == "assistant":
                return f"[你]: {row.content}"
            if row.user_id == user_id:
                return f"[评估对象 {row.user_name}]: {row.content}"
            return f"[{row.user_name}]: {row.content}"

        transcript = [_transcript_line(row) for row in recent]
        transcript.append(f"[评估对象 {user_name}]: {content}")
        if reply and reply != "[END_OF_RESPONSE]":
            transcript.append(f"[你]: {reply}")
        try:
            result = await run_evaluation(
                config,
                config.persona,
                axes,
                impression,
                memory_context.profile_facts,
                transcript,
                user_name,
                channel_id,
            )
        except Exception as e:
            _LOGGER.warning(f"relationship evaluation failed: {e!r}")
            result = None
        if result is not None:
            from .persona.eval import apply_deltas

            axes = apply_deltas(axes, result)
            impression = result.impression
            try:
                await apply_memory_updates(config, user_id, channel_id, result)
            except Exception as e:
                _LOGGER.warning(f"memory update failed: {e!r}")
            await set_mood(channel_id, mood + result.mood_delta)

    await save_relation(
        user_id,
        channel_id,
        axes=axes,
        impression=impression,
        familiarity=familiarity,
        eval_counter=counter,
    )
    return BLOCK
