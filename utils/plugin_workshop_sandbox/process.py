"""Bounded Docker CLI IO; every subprocess and pipe reader has an owner."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


class OutputLimitError(RuntimeError):
    pass


class ProcessIOError(RuntimeError):
    def __init__(self, message: str, stdout: bytes, stderr: bytes):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True)
class ProcessResult:
    code: int
    stdout: bytes
    stderr: bytes


async def settle(task: asyncio.Task):
    """Finish cleanup even if the caller receives additional cancellations."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def run_process(
    executable: str,
    *args: str,
    input_bytes: bytes | None = None,
    timeout: float = 15,
    output_bytes: int = 65536,
) -> ProcessResult:
    # Shield creation so cancellation cannot lose a just-created child handle.
    creation = asyncio.create_task(
        asyncio.create_subprocess_exec(
            executable,
            *args,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    )
    try:
        process = await asyncio.shield(creation)
    except asyncio.CancelledError:
        process = await settle(creation)
        if process.returncode is None:
            process.kill()
        await settle(asyncio.create_task(process.communicate()))
        raise
    used = 0
    captured = [bytearray(), bytearray()]

    async def read_stream(stream: asyncio.StreamReader, output: bytearray) -> bytes:
        nonlocal used
        while chunk := await stream.read(8192):
            remaining = max(0, output_bytes - used)
            output.extend(chunk[:remaining])
            used += len(chunk)
            if used > output_bytes:
                raise OutputLimitError("Docker output exceeded the configured byte limit")
        return bytes(output)

    async def send() -> None:
        if process.stdin is not None:
            try:
                process.stdin.write(input_bytes or b"")
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

    assert process.stdout is not None
    assert process.stderr is not None
    readers = [
        asyncio.create_task(read_stream(process.stdout, captured[0])),
        asyncio.create_task(read_stream(process.stderr, captured[1])),
    ]
    writer = asyncio.create_task(send())
    waiter = asyncio.create_task(process.wait())

    async def exchange() -> ProcessResult:
        stdout, stderr, _, code = await asyncio.gather(readers[0], readers[1], writer, waiter)
        return ProcessResult(code, stdout, stderr)

    async def cleanup() -> None:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        for task in (*readers, writer, waiter):
            if not task.done():
                task.cancel()
        await asyncio.gather(readers[0], readers[1], writer, waiter, return_exceptions=True)
        await process.wait()

    try:
        return await asyncio.wait_for(exchange(), timeout)
    except (asyncio.TimeoutError, OutputLimitError) as exc:
        message = "Docker command timed out" if isinstance(exc, asyncio.TimeoutError) else str(exc)
        raise ProcessIOError(message, bytes(captured[0]), bytes(captured[1])) from exc
    finally:
        await settle(asyncio.create_task(cleanup()))
