#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Ollama / 9B 清理脚本
====================
把 128K Strata 链路彻底接管后遗留的 Ollama 9B 资产清干净。

用法
----
    python cleanup-ollama-9b.py                 # 只做 dry-run，打印将要做什么（默认）
    python cleanup-ollama-9b.py --backup        # 只做备份（改配置前必跑）
    python cleanup-ollama-9b.py --config        # 改配置：ofm settings + bili routes
    python cleanup-ollama-9b.py --stop          # 停进程 + 卸载 Ollama 本体
    python cleanup-ollama-9b.py --purge         # 删目录（需二次确认输入 PURGE）
    python cleanup-ollama-9b.py --path          # 清 PATH + 启动项
    python cleanup-ollama-9b.py --archive       # 归档 ofm-effort-menu-patch 等
    python cleanup-ollama-9b.py --all           # 依次跑上面全部（不含 --purge）

设计原则
--------
1. 每一步幂等：跑两次结果一样。
2. 所有配置改动先备份到 <原文件>.bak-ollama-<时间戳>。
3. --purge 要手动输入 PURGE 才真删，防止手滑。
4. 只碰 9B / Ollama 相关，绝不碰 8090 / 8081 / strata / maid-local-* / bili 8090 provider。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

# ---------------------------------------------------------------- 常量

HOME = os.path.expanduser("~")
TS = time.strftime("%Y%m%d-%H%M%S")

OFM_SETTINGS = os.path.join(HOME, ".dsh", "our-free-model", "settings.json")
BILI_CONFIG = os.path.join(HOME, ".config", "billion-context", "billion-context.json")

OLLAMA_HOME = os.path.join(HOME, ".ollama")
OLLAMA_INSTALL = os.path.join(HOME, "AppData", "Local", "Programs", "Ollama")
OFM_DEPLOY = os.path.join(HOME, ".dsh", "our-free-model", "_deploy")

STARTUP = os.path.join(
    HOME, "AppData", "Roaming", "Microsoft", "Windows", "Start Menu",
    "Programs", "Startup",
)
STARTUP_LNK = os.path.join(STARTUP, "Ollama.lnk")

# 你的 DSH 工作区（改配置/脚本归档时从这里找残留），以及归档去向
WORKSPACE = os.environ.get("DSH_WORKSPACE", os.getcwd())
ARCHIVE = os.path.join(os.path.dirname(WORKSPACE), "ollama-9b-retired")

# 要归档的工作区残留
ARCHIVE_ITEMS = [
    "ofm-effort-menu-patch",
    "ofm-patch-baseline",
    "ofm-effort-menu-b-route-report.md",
    "_ofm_post.py",
    "ofm_api.py",
    "ofm_configure.py",
    "ofm_step32k.py",
    "ofm_step32k2.py",
    "ofm_step32k3.py",
]

# bili 里要删掉的 provider key（只删 11434，8090 与云厂商一律不动）
BILI_DROP_PROVIDERS = [
    "http://localhost:11434",
    "http://127.0.0.1:11434",
    "mitm://127.0.0.1:11434",
]

# ofm 里要删掉的 combo
OFM_DROP_COMBOS = ["combo:existing-plus-qwen-local-32k"]

DRY = True


def say(msg):
    print(msg, flush=True)


def human(n):
    return f"{n / 1024 ** 3:.2f} GiB"


def dir_size(p):
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def backup(path):
    """备份到 <path>.bak-ollama-<ts>，已存在则跳过。"""
    dst = f"{path}.bak-ollama-{TS}"
    if not os.path.exists(path):
        say(f"  [跳过] 不存在 {path}")
        return None
    if DRY:
        say(f"  [dry] 备份 {path} -> {dst}")
        return dst
    shutil.copy2(path, dst)
    say(f"  [ok] 备份 {path} -> {dst}")
    return dst


# ---------------------------------------------------------------- 1. 备份

def phase_backup():
    say("\n=== PHASE 1: 备份配置 ===")
    backup(OFM_SETTINGS)
    backup(BILI_CONFIG)
    say(f"  [info] 启动项 {STARTUP_LNK} 存在={os.path.exists(STARTUP_LNK)}")


# ---------------------------------------------------------------- 2. 改配置

def phase_config():
    say("\n=== PHASE 2: 清理配置里的 9B / 11434 ===")

    # --- ofm settings.json
    if not os.path.exists(OFM_SETTINGS):
        say(f"  [跳过] {OFM_SETTINGS} 不存在")
    else:
        d = json.load(open(OFM_SETTINGS, encoding="utf-8"))
        agg = d.get("aggregate", {})
        provs = agg.get("providers", {})

        if "ollama" in provs:
            if DRY:
                say("  [dry] settings.json: 删 aggregate.providers.ollama")
            else:
                del provs["ollama"]
                say("  [ok] settings.json: 删 aggregate.providers.ollama")
        else:
            say("  [跳过] settings.json 无 aggregate.providers.ollama")

        combos = d.get("combos", [])
        before = len(combos)
        kept = [c for c in combos if c.get("id") not in OFM_DROP_COMBOS]
        if len(kept) != before:
            if DRY:
                say(f"  [dry] settings.json: 删 {before - len(kept)} 个 combo（9B fallback 链）")
            else:
                d["combos"] = kept
                say(f"  [ok] settings.json: 删 {before - len(kept)} 个 combo（9B fallback 链）")
        else:
            say("  [跳过] settings.json 无 9B combo")

        if not DRY:
            with open(OFM_SETTINGS, "w", encoding="utf-8", newline="\n") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            say("  [ok] settings.json 已写回")

    # --- bili
    if not os.path.exists(BILI_CONFIG):
        say(f"  [跳过] {BILI_CONFIG} 不存在")
    else:
        d = json.load(open(BILI_CONFIG, encoding="utf-8"))
        provs = d.get("providers", {})
        dropped = []
        for key in BILI_DROP_PROVIDERS:
            if key in provs:
                if not DRY:
                    del provs[key]
                dropped.append(key)
        if dropped:
            say(f"  [{'dry' if DRY else 'ok'}] bili: 删 {len(dropped)} 条 11434 路由 -> {dropped}")
        else:
            say("  [跳过] bili 无 11434 路由")

        # 安全检查：8090 必须在
        if "http://127.0.0.1:8090" not in provs:
            say("  [!! 危险] 8090 provider 不见了！已中止写回")
            return
        if not DRY:
            with open(BILI_CONFIG, "w", encoding="utf-8", newline="\n") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            say("  [ok] bili 已写回（8090 保留，maxContextLimit 未动）")


# ---------------------------------------------------------------- 3. 卸载本体

def phase_stop():
    say("\n=== PHASE 3: 停进程 + 卸载 Ollama 本体 ===")

    # 3a 停进程
    r = subprocess.run(
        ["tasklist", "/fo", "csv", "/nh"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    procs = [l for l in (r.stdout or "").splitlines() if "ollama" in l.lower()]
    if procs:
        for line in procs:
            pid = line.split('","')[1]
            if DRY:
                say(f"  [dry] 结束进程 PID {pid}")
            else:
                subprocess.run(["taskkill", "/PID", pid, "/F"],
                               capture_output=True, text=True)
                say(f"  [ok] 结束进程 PID {pid}")
    else:
        say("  [跳过] 没有 ollama 进程在跑")

    # 3b 卸载
    uninst = os.path.join(OLLAMA_INSTALL, "unins000.exe")
    if not os.path.exists(uninst):
        say(f"  [跳过] 找不到卸载器 {uninst}")
        return
    if DRY:
        say(f'  [dry] 执行 "{uninst}" /SILENT')
    else:
        say("  [..] 正在静默卸载，等 60 秒…")
        subprocess.run([uninst, "/SILENT"], capture_output=True, text=True)
        time.sleep(60)
        say(f"  [ok] 卸载完成，安装目录还在={os.path.exists(OLLAMA_INSTALL)}")


# ---------------------------------------------------------------- 4. 删目录

def phase_purge():
    say("\n=== PHASE 4: 删除本地缓存目录 ===")

    targets = []
    for p in (OLLAMA_HOME, OFM_DEPLOY, OLLAMA_INSTALL):
        if os.path.exists(p):
            targets.append((p, dir_size(p)))
        else:
            say(f"  [跳过] 不存在 {p}")

    if not targets:
        say("  [跳过] 没有要删的目录")
        return

    say("\n  将要删除：")
    total = 0
    for p, s in targets:
        say(f"    {human(s):>10}  {p}")
        total += s
    say(f"    {'-' * 10}")
    say(f"    {human(total):>10}  合计\n")

    if DRY:
        say("  [dry] 未删除（真删请加 --purge 并输入 PURGE）")
        return

    ans = input("  确认删除以上目录？输入 PURGE 继续：").strip()
    if ans != "PURGE":
        say("  [中止] 输入不匹配，什么都没删")
        return

    for p, s in targets:
        try:
            shutil.rmtree(p)
            say(f"  [ok] 已删 {p} ({human(s)})")
        except Exception as e:
            say(f"  [!!] 删除失败 {p}: {e}")


# ---------------------------------------------------------------- 5. PATH + 启动项

def phase_path():
    say("\n=== PHASE 5: 清 PATH 与启动项 ===")

    # 5a 启动项
    if os.path.exists(STARTUP_LNK):
        if DRY:
            say(f"  [dry] 删启动项 {STARTUP_LNK}")
        else:
            os.remove(STARTUP_LNK)
            say(f"  [ok] 删启动项 {STARTUP_LNK}")
    else:
        say("  [跳过] 启动项已不存在")

    # 5b 用户 PATH 里的 Ollama
    import winreg
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_ALL_ACCESS
    )
    try:
        val, typ = winreg.QueryValueEx(key, "Path")
    except OSError:
        val, typ = "", winreg.REG_EXPAND_SZ
        say("  [跳过] 用户 PATH 不存在")
        return

    segs = [s for s in val.split(";") if s.strip()]
    kept = [s for s in segs if "ollama" not in s.lower()]
    removed = [s for s in segs if "ollama" in s.lower()]

    if not removed:
        say("  [跳过] 用户 PATH 里没有 Ollama")
    elif DRY:
        say(f"  [dry] 从用户 PATH 移除：{removed}")
    else:
        winreg.SetValueEx(key, "Path", 0, typ, ";".join(kept))
        say(f"  [ok] 从用户 PATH 移除：{removed}")

    # 5c OLLAMA_* 环境变量
    found = []
    i = 0
    while True:
        try:
            n, v, _t = winreg.EnumValue(key, i)
        except OSError:
            break
        i += 1
        if n.upper().startswith("OLLAMA"):
            found.append((n, v))
    if found:
        for n, v in found:
            if DRY:
                say(f"  [dry] 删环境变量 {n}={v}")
            else:
                winreg.DeleteValue(key, n)
                say(f"  [ok] 删环境变量 {n}")
    else:
        say("  [跳过] 没有 OLLAMA_* 环境变量")
    winreg.CloseKey(key)

    # 5d 广播 WM_SETTINGCHANGE（让新开的终端立刻生效）
    if not DRY:
        try:
            import ctypes
            ctypes.windll.user32.SendMessageW(
                0xFFFF, 0x001A, 0, "Environment"
            )
            say("  [ok] 已广播环境变量变更")
        except Exception:
            pass


# ---------------------------------------------------------------- 6. 归档

def phase_archive():
    say("\n=== PHASE 6: 归档 ofm 旧补丁与调试脚本 ===")

    if DRY:
        say(f"  [dry] 目标归档目录 {ARCHIVE}")
    else:
        os.makedirs(ARCHIVE, exist_ok=True)
        say(f"  [ok] 归档目录 {ARCHIVE}")

    moved = 0
    for name in ARCHIVE_ITEMS:
        src = os.path.join(WORKSPACE, name)
        if not os.path.exists(src):
            say(f"  [跳过] 不存在 {name}")
            continue
        dst = os.path.join(ARCHIVE, name)
        if DRY:
            say(f"  [dry] 移动 {name} -> {ARCHIVE}")
        else:
            shutil.move(src, dst)
            say(f"  [ok] 移动 {name}")
        moved += 1

    say(f"  [info] 共处理 {moved} 项")
    say("  [注意] ofm 插件本体不回滚：effort 补丁已应用且无害，回滚反而增加风险")


# ---------------------------------------------------------------- main

def main():
    global DRY

    ap = argparse.ArgumentParser(description="清理 Ollama / 9B 残留")
    ap.add_argument("--backup", action="store_true")
    ap.add_argument("--config", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--purge", action="store_true")
    ap.add_argument("--path", action="store_true")
    ap.add_argument("--archive", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--execute", action="store_true",
                    help="真正执行（不加则一律 dry-run）")
    a = ap.parse_args()

    DRY = not a.execute
    if DRY:
        say("*** DRY-RUN 模式：只打印，不动任何东西。真跑加 --execute ***\n")

    if a.all:
        a.backup = a.config = a.stop = a.path = a.archive = True

    if not any([a.backup, a.config, a.stop, a.purge, a.path, a.archive]):
        say("什么都没指定，跑一遍全量 dry-run 预览：\n")
        DRY = True
        a.backup = a.config = a.stop = a.purge = a.path = a.archive = True

    if a.backup:
        phase_backup()
    if a.config:
        phase_config()
    if a.stop:
        phase_stop()
    if a.purge:
        phase_purge()
    if a.path:
        phase_path()
    if a.archive:
        phase_archive()

    say("\n=== 完成 ===")


if __name__ == "__main__":
    main()
