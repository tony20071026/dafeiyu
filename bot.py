#!/usr/bin/env python3
"""大肥鱼 —— QQ Open Platform bot entry point."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.metadata
import logging
import signal
import sys

import aiohttp

from config import Settings, settings
from handlers.message_handler import MessageHandler
from utils.ai import AIClient
from utils.api import QQApiClient
from utils.auth import TokenError, TokenManager
from utils.context_window import ContextWindowManager
from utils.gateway import QQGatewayClient
from utils.logging_setup import setup_logging
from utils.memes import MemeLibrary
from utils.web_fetch import WebFetcher

LOGGER = logging.getLogger("dafeiyu")


def _mask_app_id(app_id: str) -> str:
    if not app_id:
        return "<empty>"
    if len(app_id) <= 4:
        return "***"
    return f"***{app_id[-4:]}"


def _thinking_label(value: bool | None) -> str:
    if value is None:
        return "默认"
    return "开启" if value else "关闭"


def _read_check() -> int:
    setup_logging(settings.log_level, settings.log_dir)
    print("大肥鱼环境自检")
    print(f"  Python      : {sys.version.split()[0]}")
    print(f"  工作目录    : {settings.assets_dir.parent}")
    print(f"  .env        : {settings.assets_dir.parent / '.env'}")
    print(f"  APP_ID      : {_mask_app_id(settings.app_id)}")
    print(f"  APP_SECRET  : {'已配置' if settings.app_secret else '未配置'}")
    print(f"  API Base    : {settings.api_base}")
    print(f"  Token URL   : {settings.token_url}")
    print(f"  Intents     : {settings.intents}")
    print(f"  上下文选项  : {settings.context_default} / {settings.context_options}")
    print(f"  表情包目录  : {settings.memes_dir}")
    print(
        "  AI          : "
        f"{'启用' if settings.ai_enabled else '关闭'}"
        f" / 回复模型 {settings.ai_model}"
        f" / 判断模型 {settings.ai_judge_model}"
        f" / 思考 {_thinking_label(settings.ai_enable_thinking)}"
        f" / Key {'已配置' if settings.ai_api_key else '未配置'}"
    )
    print(f"  固定回复    : {'启用' if settings.fixed_replies_enabled else '关闭'}")
    print(
        "  群聊判断    : "
        f"{'启用' if settings.group_reply_judge else '关闭'}"
        f" / 判断上下文 {settings.group_judge_context_chars} 字"
        f" / 冷却 {settings.group_reply_cooldown:g}s"
        f" / 潜水 {settings.group_idle_speak_every or '关闭'}"
    )
    print(
        "  表情包库    : "
        f"自动收集 {'开' if settings.meme_auto_collect else '关'}"
        f" / 主动发送 {'开' if settings.meme_send_enabled else '关'}"
        f" / 识别 {'开' if settings.meme_analyze else '关'}"
        f" / 上限 {settings.meme_max_files} 张"
    )

    dependency_errors: list[str] = []
    for module, package in (
        ("aiohttp", "aiohttp"),
        ("aiofiles", "aiofiles"),
        ("websockets", "websockets"),
        ("dotenv", "python-dotenv"),
    ):
        try:
            importlib.import_module(module)
            version = importlib.metadata.version(package)
            print(f"  依赖 {package:<13}: {version}")
        except Exception as exc:
            dependency_errors.append(f"{package}: {exc}")
            print(f"  依赖 {package:<13}: 缺失/不可用（{exc}）")

    try:
        importlib.import_module("PIL")
        print(f"  可选 Pillow      : {importlib.metadata.version('Pillow')}")
    except Exception:
        print("  可选 Pillow      : 未安装（表情包生成将使用标准库回退）")

    missing = [
        name
        for name, value in (("APP_ID", settings.app_id), ("APP_SECRET", settings.app_secret))
        if not value
    ]
    if missing:
        print(f"\n[失败] 缺少凭据：{', '.join(missing)}，请编辑 .env 后重试。")
        return 1
    if dependency_errors:
        print(f"\n[失败] 依赖异常：{'; '.join(dependency_errors)}")
        return 1
    print("\n[通过] 配置与依赖可正常导入。")
    return 0


async def _run(settings: Settings, stop_event: asyncio.Event) -> int:
    settings.require_credentials()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.memes_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    LOGGER.info("=" * 56)
    LOGGER.info("大肥鱼启动中 | APP_ID=%s", _mask_app_id(settings.app_id))
    LOGGER.info(
        "上下文窗口默认=%s，可选=%s",
        settings.context_default,
        settings.context_options,
    )
    LOGGER.info("表情包目录=%s", settings.memes_dir)
    LOGGER.info(
        "消息模式：群聊全量/群@、频道@、私聊 C2C 已启用"
        "（群聊全量需在开放平台开启“接收所有消息”）"
    )
    LOGGER.info(
        "AI：%s / 回复模型=%s / 判断模型=%s / 思考=%s / 固定回复=%s",
        "启用" if settings.ai_enabled else "关闭",
        settings.ai_model,
        settings.ai_judge_model,
        _thinking_label(settings.ai_enable_thinking),
        "启用" if settings.fixed_replies_enabled else "关闭",
    )
    LOGGER.info(
        "群聊回复策略：%s",
        (
            f"先由 AI 判断重要性（上下文 {settings.group_judge_context_chars} 字，"
            f"冷却 {settings.group_reply_cooldown:g}s），"
            f"连续 {settings.group_idle_speak_every} 条没回复时主动插话，"
            f"{settings.group_water_hours:g} 小时未发言时主动水群，"
            "@机器人 与指令直接回复"
            if settings.group_reply_judge
            else "每条普通消息都回复（已关闭重要性判断）"
        ),
    )
    LOGGER.info("=" * 56)

    timeout = aiohttp.ClientTimeout(total=settings.http_timeout)
    connector = aiohttp.TCPConnector(limit=32, ttl_dns_cache=300)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        tokens = TokenManager(settings, session)
        try:
            # Fail fast with a clear message instead of entering the reconnect loop
            # with visibly wrong credentials.
            await tokens.get_token()
        except TokenError as exc:
            LOGGER.critical("%s", exc)
            return 1

        api = QQApiClient(settings, session, tokens)
        contexts = ContextWindowManager(
            settings.context_state_file,
            default_size=settings.context_default,
            options=settings.context_options,
        )
        await contexts.load()

        ai_client: AIClient | None = None
        if settings.ai_enabled:
            if settings.ai_api_key:
                ai_client = AIClient(settings, session)
            else:
                LOGGER.warning("AI_ENABLED=true 但 AI_API_KEY 为空，AI 不会工作")

        meme_library = MemeLibrary(settings, session, ai_client)
        await meme_library.load()
        total_memes, ready_memes = meme_library.stats()
        LOGGER.info(
            "表情包库：%s 张（已识别 %s 张）| 目录=%s",
            total_memes,
            ready_memes,
            settings.memes_dir,
        )
        meme_task = asyncio.create_task(
            meme_library.analyze_pending(), name="meme-index"
        )

        web_fetcher: WebFetcher | None = None
        if (
            settings.web_fetch_enabled
            and settings.web_fetch_max_chars > 0
            and ai_client is not None
        ):
            web_fetcher = WebFetcher(settings, session)
            LOGGER.info(
                "网页读取：开启（仅 @/私聊/引用机器人时；单次最多 1 个链接，"
                "正文 %s 字）",
                settings.web_fetch_max_chars,
            )
        else:
            LOGGER.info("网页读取：关闭")

        handler = MessageHandler(
            settings,
            api,
            contexts,
            ai_client,
            meme_library,
            web_fetcher,
        )
        water_task: asyncio.Task[None] | None = None
        if settings.group_water_hours > 0:
            water_task = asyncio.create_task(
                handler.run_water_timer(stop_event), name="group-water-timer"
            )
        gateway = QQGatewayClient(settings, api, tokens, handler)

        refresh_task = asyncio.create_task(
            tokens.auto_refresh_loop(stop_event), name="token-refresh"
        )
        gateway_task = asyncio.create_task(gateway.run(stop_event), name="qq-gateway")

        def _gateway_finished(task: asyncio.Task[None]) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                LOGGER.critical(
                    "网关任务意外退出", exc_info=(type(exc), exc, exc.__traceback__)
                )
            if not stop_event.is_set():
                LOGGER.warning("网关任务已结束，开始关闭机器人")
                stop_event.set()

        gateway_task.add_done_callback(_gateway_finished)

        LOGGER.info("大肥鱼已进入事件循环，按 Ctrl+C 优雅退出")
        try:
            await stop_event.wait()
        finally:
            LOGGER.info("正在关闭大肥鱼…")
            stop_event.set()
            await gateway.stop()
            try:
                await asyncio.wait_for(gateway_task, timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                gateway_task.cancel()
                await asyncio.gather(gateway_task, return_exceptions=True)
            refresh_task.cancel()
            meme_task.cancel()
            shutdown_tasks: list[asyncio.Task[None]] = [refresh_task, meme_task]
            if water_task is not None:
                water_task.cancel()
                shutdown_tasks.append(water_task)
            await asyncio.gather(*shutdown_tasks, return_exceptions=True)

    LOGGER.info("大肥鱼已下线，拜拜 🐟")
    return 0


async def _async_main(settings: Settings) -> int:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown() -> None:
        LOGGER.info("收到退出信号")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:  # pragma: no cover - non-Unix
            pass

    try:
        return await _run(settings, stop_event)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(sig)
            except NotImplementedError:  # pragma: no cover
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="大肥鱼 QQ 机器人")
    parser.add_argument(
        "--check",
        action="store_true",
        help="检查依赖、凭据和路径，不连接 QQ 网关",
    )
    args = parser.parse_args()

    if args.check:
        return _read_check()

    setup_logging(settings.log_level, settings.log_dir)
    try:
        return asyncio.run(_async_main(settings))
    except RuntimeError as exc:
        LOGGER.critical("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.info("收到 Ctrl+C，正在退出")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
