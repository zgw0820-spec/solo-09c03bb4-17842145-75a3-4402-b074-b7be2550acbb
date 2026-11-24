#!/usr/bin/env python3
"""中断恢复与启动清理的端到端测试（对应缺陷修复）。

场景：
A. 对象迁入后、分支发布前强停：重启后单传依赖该批提交的增量包，
   必须整批退回并列出缺失的完整 SHA；启动清理会删掉遗留临时引用。
B. 场景 A 中临时引用删不掉（清理失败）：网页仍能查看现行分支，
   前置齐全的包仍能导入，残留引用不得充当验包依据。
C. 分支已发布后强停（事务成功、临时引用清理未跑）：已发布分支保留，
   后续包可以依赖它们；启动清理只删 refs/import/*，不碰其他引用、不碰对象。
D. 前置已由正式分支满足时，即便对象库里有“游离”对象也一切照常（正向对照）。
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
BUNDLES = os.path.join(SAMPLES, "bundles")
FAILED = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILED.append(name)


def fresh_repo():
    d = tempfile.mkdtemp(prefix="crash-repo-")
    repo = os.path.join(d, "repo.git")
    subprocess.run(["git", "clone", "-q", "--bare",
                    os.path.join(SAMPLES, "repo.git"), repo], check=True)
    return d, repo


def load(name):
    with open(os.path.join(BUNDLES, name), "rb") as fh:
        return [(name, fh.read())]


def head(repo, branch):
    p = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "-q",
                        f"refs/heads/{branch}"], capture_output=True, text=True)
    return p.stdout.strip() or None


def tip(name, branch):
    return m.parse_bundle(os.path.join(BUNDLES, name), name).heads[
        f"refs/heads/{branch}"]


def simulate_crash_after_fetch(repo, bundle_name):
    """只做“对象迁入正式库临时引用空间”，不发布分支（模拟强停）。"""
    work = tempfile.mkdtemp(prefix="crash-work-")
    q = os.path.join(work, "q.git")
    m.git(None, "init", "-q", "--bare", q)
    info = os.path.join(q, "objects", "info")
    os.makedirs(info, exist_ok=True)
    with open(os.path.join(info, "alternates"), "w") as fh:
        fh.write(os.path.join(os.path.abspath(repo), "objects") + "\n")
    for ref, sha in m.list_persistent_refs(repo).items():
        m.git(q, "update-ref", ref, sha, check=False)
    path = os.path.join(BUNDLES, bundle_name)
    m.git(q, "bundle", "verify", path)
    b = m.parse_bundle(path, bundle_name)
    m.git(q, "fetch", "--no-tags", "--", path,
          "+refs/heads/*:refs/q/0/heads/*")
    batch = uuid.uuid4().hex[:12]
    m.git(repo, "fetch", "--no-tags", "--", q,
          f"+refs/q/*:refs/import/{batch}/*")
    shutil.rmtree(work, ignore_errors=True)
    return batch, b


def fresh_process():
    """模拟服务重启：进程级一次性清理标志复位。"""
    m._startup_cleanup_done.clear()


B01, B02, B03 = ("01-main-c4-feature-f1.bundle",
                 "02-main-c5.bundle",
                 "03-feature-f2.bundle")
# C4 是包 02 声明的前置提交（由包 01 提供），不是 02 的分支尖端
C4 = m.parse_bundle(os.path.join(BUNDLES, B02), B02).prereqs[0][0]
F1 = m.parse_bundle(os.path.join(BUNDLES, B03), B03).prereqs[0][0]

# ================= A. 发布前强停：增量包必须退回并列出完整 SHA ==============
d, repo = fresh_repo()
try:
    main0, stable0 = head(repo, "main"), head(repo, "stable")
    simulate_crash_after_fetch(repo, B01)
    leftover_before = m.list_refs(repo, m.TMP_REFS_PREFIX)
    check("A1 强停后遗留临时引用存在", len(leftover_before) == 2,
          str(list(leftover_before)))
    check("A2 强停后正式分支未发布（main 仍是 C2）", head(repo, "main") == main0)

    fresh_process()  # 模拟重启
    r = m.import_bundles(repo, load(B02))
    check("A3 单传 02 被整批退回", not r["ok"], r.get("reason") or "")
    listed = r["missing"] and any(
        sha == C4 for miss in r["missing"].values()
        for sha, _ in miss)
    check("A4 退回原因列出缺失的完整 SHA（C4）", listed, str(r["missing"]))
    check("A5 退回后 main 未被推进", head(repo, "main") == main0)
    check("A6 启动清理已删除遗留临时引用",
          not m.list_refs(repo, m.TMP_REFS_PREFIX))
    check("A7 无关分支 stable 未受影响", head(repo, "stable") == stable0)

    # 清理后补齐 01 再传 02，应恢复正常
    r = m.import_bundles(repo, load(B01))
    check("A8 补传 01 成功并发布 main=C4/feature=F1",
          r["ok"] and head(repo, "main") == tip(B01, "main")
          and head(repo, "feature") == tip(B01, "feature"),
          r.get("reason") or "")
    r = m.import_bundles(repo, load(B02))
    check("A9 再传 02 成功，main 推进到 C5",
          r["ok"] and head(repo, "main") == tip(B02, "main"),
          r.get("reason") or "")
finally:
    shutil.rmtree(d, ignore_errors=True)

# ================= B. 清理失败：残留引用不能充当验包依据 ====================
d, repo = fresh_repo()
try:
    main0 = head(repo, "main")
    simulate_crash_after_fetch(repo, B01)
    check("B1 制造了遗留临时引用",
          bool(m.list_refs(repo, m.TMP_REFS_PREFIX)))

    # 让临时引用删除失败（模拟临时引用一时删不掉）。
    # 生产代码逐个执行 `update-ref -d <ref>`（不再用 --stdin 事务）。
    real_git = m.git

    def git_no_delete(rp, *args, **kwargs):
        if args and args[0] == "update-ref" and "-d" in args:
            raise PermissionError("模拟清理失败：临时引用删不掉")
        return real_git(rp, *args, **kwargs)

    fresh_process()
    m.git = git_no_delete
    try:
        m.ensure_startup_cleanup(repo)  # 不应抛出
    finally:
        m.git = real_git
    # 清理失败时一次性标志仍已置位（服务继续运行，不反复重试阻塞）
    check("B2 清理失败不抛异常，残留引用仍在",
          bool(m.list_refs(repo, m.TMP_REFS_PREFIX))
          and m._startup_cleanup_done.is_set())

    # 网页上下文仍可读取现行分支（只显示 refs/heads/*，不含临时引用）
    ctx_branches = m.list_branches(repo)
    check("B3 网页仍能查看现行分支且不显示临时引用",
          set(ctx_branches) == {"refs/heads/main", "refs/heads/stable"},
          str(ctx_branches))

    # 残留引用在：单传 02 仍必须退回（不能拿 refs/import/* 当依据）
    r = m.import_bundles(repo, load(B02))
    check("B4 残留引用不充当验包依据，02 仍被退回",
          not r["ok"] and r["missing"] and any(
              sha == C4 for miss in r["missing"].values()
              for sha, _ in miss),
          r.get("reason") or "")
    check("B5 退回后分支不动", head(repo, "main") == main0)

    # 前置齐全的包（01 的前置 C2 在正式分支上）仍能正常导入
    r = m.import_bundles(repo, load(B01))
    check("B6 前置齐全的 01 仍可正常导入", r["ok"]
          and head(repo, "main") == tip(B01, "main"), r.get("reason") or "")
    r = m.import_bundles(repo, load(B02))
    check("B7 01 发布后 02 可正常导入", r["ok"]
          and head(repo, "main") == tip(B02, "main"), r.get("reason") or "")
    # 正常导入结束后自己的临时引用已清，历史残留仍在（删不掉的那批）
    check("B8 正常流程不被残留引用干扰",
          head(repo, "feature") == tip(B01, "feature"))
finally:
    shutil.rmtree(d, ignore_errors=True)

# ============= C. 发布后强停：已发布分支保留，后续包可依赖 ==================
d, repo = fresh_repo()
try:
    # 完整导入 01（含发布），然后人为再制造一个“已发布但临时引用没清”的现场：
    # 即直接模拟 fetch 后事务成功、进程在 finally 清理前被杀。
    r = m.import_bundles(repo, load(B01))
    check("C1 01 正常导入成功", r["ok"] and head(repo, "main") == tip(B01, "main"))
    # 制造一个孤儿临时引用（指向已发布的同一提交，模拟清理步骤没跑）
    m.git(repo, "update-ref",
          "refs/import/orphan/0/heads/main", tip(B01, "main"))
    n_obj_before = sum(len(files) for _, _, files in os.walk(
        os.path.join(repo, "objects")))

    fresh_process()
    m.ensure_startup_cleanup(repo)
    check("C2 启动清理只删 refs/import/*",
          not m.list_refs(repo, m.TMP_REFS_PREFIX)
          and head(repo, "main") == tip(B01, "main")
          and head(repo, "feature") == tip(B01, "feature"))
    n_obj_after = sum(len(files) for _, _, files in os.walk(
        os.path.join(repo, "objects")))
    check("C3 清理不扫描/删除对象（对象数量不变）", n_obj_before == n_obj_after,
          f"{n_obj_before} -> {n_obj_after}")

    # 后续包依赖已发布分支：02 前置 C4 现由 refs/heads/main 背书
    r = m.import_bundles(repo, load(B02))
    check("C4 后续包 02 可依赖已发布分支导入",
          r["ok"] and head(repo, "main") == tip(B02, "main"),
          r.get("reason") or "")
    r = m.import_bundles(repo, load(B03))
    check("C5 后续包 03 可依赖已发布 feature 导入",
          r["ok"] and head(repo, "feature") == tip(B03, "feature"),
          r.get("reason") or "")
    check("C6 全程无临时引用残留",
          not m.list_refs(repo, m.TMP_REFS_PREFIX))
finally:
    shutil.rmtree(d, ignore_errors=True)

# ================= D. 正向对照：乱序整批仍自动排序成功 ======================
d, repo = fresh_repo()
try:
    files = []
    for n in (B03, B02, B01):
        with open(os.path.join(BUNDLES, n), "rb") as fh:
            files.append((n, fh.read()))
    r = m.import_bundles(repo, files)
    seq = [os.path.basename(x["file"]) for x in r["order"]]
    check("D1 乱序整批仍自动排序并成功", r["ok"]
          and seq.index(B01) < seq.index(B02)
          and seq.index(B01) < seq.index(B03), str(seq))
    check("D2 最终 main=C5、feature=F2",
          head(repo, "main") == tip(B02, "main")
          and head(repo, "feature") == tip(B03, "feature"))
finally:
    shutil.rmtree(d, ignore_errors=True)

print()
if FAILED:
    print(f"{len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("全部中断恢复测试通过")
