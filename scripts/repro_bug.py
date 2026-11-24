#!/usr/bin/env python3
"""复现缺陷：对象迁入后、分支发布前被强停，重启后单传增量包误报成功。

步骤：
1. 正常导入包 01（main->C4, feature->F1），但在 fetch 进正式库临时引用之后、
   update-ref 事务之前模拟强杀（直接调用内部步骤，留下 refs/import/<batch>/*）。
2. 重启后只上传包 02（前置 C4）。正确行为：C4 只能从 refs/import/* 临时引用到达，
   不算数 -> 整批退回并列出 C4 完整 SHA。缺陷行为：误报成功。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import main as m  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")


def fresh_repo():
    d = tempfile.mkdtemp(prefix="repro-repo-")
    repo = os.path.join(d, "repo.git")
    subprocess.run(["git", "clone", "-q", "--bare",
                    os.path.join(SAMPLES, "repo.git"), repo], check=True)
    return d, repo


def load(name):
    with open(os.path.join(SAMPLES, name), "rb") as fh:
        return [(os.path.basename(name), fh.read())]


def head(repo, branch):
    p = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "-q",
                        f"refs/heads/{branch}"], capture_output=True, text=True)
    return p.stdout.strip() or None


d, repo = fresh_repo()
try:
    # --- 模拟强杀：手动执行 import_bundles 第 6 步的 fetch，但不做分支发布 ---
    work = tempfile.mkdtemp(prefix="repro-work-")
    q = os.path.join(work, "q.git")
    m.git(None, "init", "-q", "--bare", q)
    info = os.path.join(q, "objects", "info")
    os.makedirs(info, exist_ok=True)
    with open(os.path.join(info, "alternates"), "w") as fh:
        fh.write(os.path.join(os.path.abspath(repo), "objects") + "\n")
    for ref, sha in m.list_refs(repo).items():
        m.git(q, "update-ref", ref, sha, check=False)
    b01_path = os.path.join(SAMPLES, "bundles",
                            "01-main-c4-feature-f1.bundle")
    m.git(q, "bundle", "verify", b01_path)
    m.git(q, "fetch", "--no-tags", "--", b01_path,
          "+refs/heads/*:refs/q/0/heads/*")
    batch = uuid.uuid4().hex[:12]
    m.git(repo, "fetch", "--no-tags", "--", q,
          f"+refs/q/*:refs/import/{batch}/*")
    shutil.rmtree(work, ignore_errors=True)

    leftover = m.list_refs(repo, m.TMP_REFS_PREFIX)
    print("强杀后正式库遗留临时引用：")
    for ref, sha in leftover.items():
        print(f"  {ref} -> {sha}")
    print("正式库分支（强杀前未发布，应仍是初始状态）：",
          m.list_branches(repo))
    main_before = head(repo, "main")

    # --- “重启”后单传包 02（前置 C4 仅由遗留临时引用可达） ---
    # 模拟新进程：启动清理标志是进程级一次性状态，复位后首次导入会先清理。
    m._startup_cleanup_done.clear()
    r = m.import_bundles(repo, load("bundles/02-main-c5.bundle"))
    print("\n单传 02 的结果：", "ok=True" if r["ok"] else "ok=False",
          "| reason:", r["reason"])
    print("missing:", r["missing"])
    print("main 现在指向：", head(repo, "main"))

    c4 = m.parse_bundle(os.path.join(SAMPLES, "bundles",
                                     "02-main-c5.bundle"), "x").prereqs[0][0]
    if r["ok"]:
        print(f"\n[缺陷复现] 误报导入成功！C4={c4} 仅能从 refs/import/* 到达，"
              "main 被错误推进到", head(repo, "main"))
        sys.exit(1)
    listed = r["missing"] and any(
        sha == c4 for miss in r["missing"].values() for sha, _ in miss)
    if listed and head(repo, "main") == main_before:
        print(f"\n[修复正确] 整批退回并列出缺失 C4={c4}，分支未动。")
        sys.exit(0)
    print("\n[异常] 退回了但缺失 SHA 列表或分支状态不对。")
    sys.exit(2)
finally:
    shutil.rmtree(d, ignore_errors=True)
