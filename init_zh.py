r"""首次全量翻译：把所有段落的英文译成中文，写入 state/zh_map.json。

用法：python3 init_zh.py [provider]
断点续跑：已 synced 的段落会跳过，中断后重跑即可。
"""

from __future__ import annotations

import asyncio
import sys

import translator
from app import RT


async def main(provider: str | None = None):
    if not RT.ready:
        RT.init(translator.load_config())
    cfg = translator.load_config()
    state = RT.state
    TEX_FILES = RT.tex_files
    client, model, name = translator.get_client(cfg, provider)
    print(f"使用 provider: {name} ({model})")

    todo = []
    for rel in TEX_FILES:
        segs = state.refresh_file(rel)
        for s in segs:
            if s["status"] in ("untranslated", "en_changed"):
                todo.append((rel, s))
    print(f"待翻译 {len(todo)} 段 / 共 {sum(len(state.segs(r)) for r in TEX_FILES)} 段")

    sem = asyncio.Semaphore(4)
    done = 0
    lock = asyncio.Lock()

    async def one(rel, rec):
        nonlocal done
        async with sem:
            for attempt in range(3):
                try:
                    rec["zh"] = await translator.translate(rec["en"], "en2zh", cfg, provider)
                    rec["status"] = "synced"
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 2:
                        rec["status"] = "untranslated"
                        print(f"  ✗ {rel}#{rec['idx']}: {e}")
                    await asyncio.sleep(3 * (attempt + 1))
            async with lock:
                done += 1
                if done % 10 == 0 or done == len(todo):
                    state.save()
                    print(f"  进度 {done}/{len(todo)}")

    await asyncio.gather(*(one(rel, s) for rel, s in todo))
    state.save()
    n_ok = sum(1 for r in TEX_FILES for s in state.segs(r) if s["status"] == "synced")
    print(f"完成：{n_ok} 段已有中文。启动编辑器：python3 app.py")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
