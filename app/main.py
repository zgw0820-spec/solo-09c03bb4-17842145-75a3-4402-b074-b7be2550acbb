"""Git bundle 批量导入服务。

把多份（至多 6 份）乱序的 git bundle 导入本地裸仓库：

* 按各包声明的前置提交（prerequisite）自动排出可用的验包顺序；
* 缺包时指出缺失的提交（完整 SHA）；
* 只更新分支（refs/heads/*），标签等其它引用一律忽略；
* 仓库与各包中的同名分支必须能排成同一条快进链，生效后指向链尾；
* 验包在带 alternates 的隔离仓库里进行：照常读取现行分支，不复制已有对象库；
* 全部包通过校验后才一起更新涉及的分支；验包期间分支已变或任一包校验失败，
  整批退回，仓库里已生效的分支保持原状。

崩溃安全（对象迁入后、分支发布前被强停的场景）：

* 前置提交必须能从正式库的“非临时引用”或本批已验包取入的分支到达；
  仅存在于对象库、或只能从遗留的 refs/import/* 临时引用到达的提交一律视为缺失，
  整批退回并列出完整 SHA（git bundle verify 只查对象存在、不查引用可达，
  因此验包前有一道显式可达性门禁，不能只看 verify 的返回码）；
* 进程启动（首次访问惰性触发）时逐个清理遗留的 refs/import/*，只删该引用空间，
  不碰其他引用、不扫描也不复制对象库；删除失败仅记日志、不阻断服务，
  残留引用也永远不会成为验包依据；
* 停机前已发布的 refs/heads/* 不受影响，后续包可正常依赖它们。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

REPO_PATH = os.environ.get("REPO_PATH", "/srv/repo.git")
SAMPLES_DIR = os.environ.get("SAMPLES_DIR", "")
SAMPLES_BAD_DIR = os.environ.get("SAMPLES_BAD_DIR", "")
MAX_BUNDLES = 6
ZERO_SHA = "0" * 40
HEADS_PREFIX = "refs/heads/"
TMP_REFS_PREFIX = "refs/import/"  # 对象迁入正式库时使用的临时引用空间

app = FastAPI(title="Git bundle 批量导入")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["short"] = lambda s: (s or "")[:12]
import_lock = threading.Lock()
logger = logging.getLogger("bundle-import")
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_handler)
# 自带 handler，不再向 root 传播，避免与 uvicorn 的日志配置重复打印
logger.propagate = False

# 启动清理只做一次：清理上次导入在对象迁入后、分支发布前被强停而遗留的临时引用。
_startup_cleanup_done = threading.Event()
_startup_cleanup_lock = threading.Lock()


# --------------------------------------------------------------------------
# git 基础操作
# --------------------------------------------------------------------------

class GitError(Exception):
    def __init__(self, args, stderr):
        super().__init__(f"git {' '.join(args)}: {stderr}")
        self.stderr = stderr


def git(repo, *args, input_text=None, check=True):
    cmd = ["git"]
    if repo:
        cmd += ["-C", str(repo)]
    cmd += [str(a) for a in args]
    proc = subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and proc.returncode != 0:
        raise GitError(cmd, proc.stderr.strip())
    return proc


def list_refs(repo, prefix=""):
    """返回 {完整引用名: sha}。prefix 如 'refs/heads/'；空前缀列出全部引用。

    注意不能把空字符串作为参数传给 git：for-each-ref 会把它当成匹配空的
    引用模式，rc=0 但无任何输出。
    """
    args = ["for-each-ref", "--format=%(refname)%00%(objectname)"]
    if prefix:
        args.append(prefix)
    out = git(repo, *args, check=False).stdout
    refs = {}
    for line in out.splitlines():
        if "\x00" in line:
            ref, sha = line.split("\x00", 1)
            refs[ref] = sha
    return refs


def list_branches(repo):
    return list_refs(repo, HEADS_PREFIX)


def list_persistent_refs(repo):
    """正式库的非临时引用。

    前置提交只允许从这些引用、或本批已验包取入的分支到达；
    refs/import/* 是导入中途（对象已迁入、分支未发布）使用的临时空间，
    进程被强停后可能残留，绝不能充当验包依据。
    """
    return {ref: sha for ref, sha in list_refs(repo).items()
            if not ref.startswith(TMP_REFS_PREFIX)}


def delete_refs_individually(repo, refs):
    """逐个删除引用并返回失败列表 [(ref, stderr)]。

    不用 update-ref --stdin：它把整批命令放进同一事务，任何一个删除失败
    （如目录只读）会导致整笔中止、其余引用也删不掉。逐个删除可隔离失败。
    """
    failed = []
    for ref in refs:
        proc = git(repo, "update-ref", "-d", ref, check=False)
        if proc.returncode != 0:
            failed.append((ref, proc.stderr.strip()))
    return failed


def cleanup_stale_tmp_refs(repo):
    """删除遗留的 refs/import/* 临时引用（启动时调用一次）。

    只动 refs/import/ 这一个引用空间，不碰其他引用，不扫描、不复制、
    不删除任何对象。临时引用一时删不掉（权限/IO 问题等）也不抛出：
    验包逻辑本身已排除该空间，残留引用不会被当作前置可达依据。
    """
    try:
        stale = sorted(list_refs(repo, TMP_REFS_PREFIX))
    except Exception as exc:  # 仓库尚未初始化等：无事可清
        logger.warning("启动清理：无法读取仓库 %s：%s", repo, exc)
        return
    if not stale:
        return
    try:
        failed = delete_refs_individually(repo, stale)
    except Exception as exc:
        # 权限/IO 等任何意外都不阻断启动；验包逻辑本身已排除该引用空间。
        logger.error("启动清理：删除遗留临时引用时出错（共 %d 项），"
                     "已忽略且这些引用不会被当作验包依据：%r",
                     len(stale), exc)
        return
    if failed:
        # 不阻断服务：隔离验包只镜像非临时引用，残留项不参与验包。
        logger.error("启动清理：%d 个遗留临时引用删除失败（共 %d 项），"
                     "它们不会被当作验包依据。首条错误：%s",
                     len(failed), len(stale), failed[0][1])
    else:
        logger.info("启动清理：已删除 %d 个遗留临时引用（refs/import/*）",
                    len(stale))


def ensure_startup_cleanup(repo):
    """进程内仅执行一次的启动清理（首次访问时惰性触发，避免 import 副作用）。"""
    if _startup_cleanup_done.is_set():
        return
    with _startup_cleanup_lock:
        if _startup_cleanup_done.is_set():
            return
        cleanup_stale_tmp_refs(repo)
        _startup_cleanup_done.set()


def is_ancestor(repo, old, new):
    """old 是否为 new 的祖先（对象不存在时视为否）。"""
    return git(repo, "merge-base", "--is-ancestor", old, new,
               check=False).returncode == 0


# --------------------------------------------------------------------------
# bundle 解析与校验
# --------------------------------------------------------------------------

HEX_RE = re.compile(r"^[0-9a-f]{40,64}$")


@dataclass
class Bundle:
    name: str                                  # 原始上传文件名
    path: str                                  # 落盘路径
    prereqs: list = field(default_factory=list)  # [(sha, 注释)]
    heads: dict = field(default_factory=dict)    # refs/heads/* -> sha
    ignored: dict = field(default_factory=dict)  # 其它引用（标签等，不导入）


def parse_bundle(path, name):
    """直接解析 bundle 文件头，取出前置提交与引用列表。"""
    prereqs, heads, ignored = [], {}, {}
    with open(path, "rb") as fh:
        first = fh.readline()
        if not first.startswith(b"# v") or b"git bundle" not in first:
            raise ValueError("不是有效的 git bundle 文件")
        for raw in fh:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if not line or line.startswith("PACK"):
                break  # 头部结束，后面是 pack 数据
            if line.startswith("@"):  # v3 能力行，如 @object-format=sha256
                continue
            if line.startswith("-"):
                parts = line[1:].split(" ", 1)
                prereqs.append((parts[0], parts[1] if len(parts) > 1 else ""))
                continue
            parts = line.split(" ")
            if len(parts) == 2 and HEX_RE.match(parts[0]):
                sha, ref = parts
                if ref.startswith(HEADS_PREFIX):
                    heads[ref] = sha
                else:
                    ignored[ref] = sha
            else:
                break
    return Bundle(name=name, path=path, prereqs=prereqs, heads=heads,
                  ignored=ignored)


def missing_prereqs(repo, bundle):
    """前置提交中未能从 repo 任一引用到达的列表（[(sha, 注释)]）。"""
    tips = list(list_refs(repo).values())
    missing = []
    for sha, comment in bundle.prereqs:
        if not any(is_ancestor(repo, sha, tip) for tip in tips):
            missing.append((sha, comment))
    return missing


def describe(bundle):
    return {
        "file": bundle.name,
        "prereqs": bundle.prereqs,
        "refs": {ref[len(HEADS_PREFIX):]: sha for ref, sha in bundle.heads.items()},
        "ignored": bundle.ignored,
    }


# --------------------------------------------------------------------------
# 导入主流程
# --------------------------------------------------------------------------

def import_bundles(repo, files):
    """把 files=[(文件名, 内容)] 的一批 bundle 导入 repo，返回结果字典。"""
    result = {"ok": False, "order": [], "updates": [], "unchanged": [],
              "reason": None, "missing": None}
    if not files:
        result["reason"] = "未选择任何包。"
        return result
    if len(files) > MAX_BUNDLES:
        result["reason"] = (f"一次最多导入 {MAX_BUNDLES} 份包，本次收到 "
                            f"{len(files)} 份，整批退回。")
        return result

    with import_lock:
        # 进程首次导入前清理上次强停遗留的临时引用（删不掉也不影响验包正确性）
        ensure_startup_cleanup(repo)
        work = tempfile.mkdtemp(prefix="bundle-import-")
        tmp_refs_created = False
        batch = None
        try:
            # 1) 落盘并解析包头
            bundles = []
            bundle_dir = os.path.join(work, "bundles")
            os.makedirs(bundle_dir)
            for i, (fname, data) in enumerate(files):
                safe = re.sub(r"[^\w.()-]", "_", os.path.basename(fname or ""))
                safe = safe or f"bundle-{i}"
                path = os.path.join(bundle_dir, f"{i:02d}-{safe}")
                with open(path, "wb") as fh:
                    fh.write(data)
                try:
                    bundles.append(parse_bundle(path, fname or safe))
                except ValueError as exc:
                    result["reason"] = f"包 {fname} 无法解析：{exc}。整批退回。"
                    return result
            for b in bundles:
                if not b.heads:
                    result["reason"] = (f"包 {b.name} 不含任何分支"
                                        f"（{HEADS_PREFIX}*），只更新分支，整批退回。")
                    return result

            # 2) 记录现行分支，并搭建隔离仓库：
            #    alternates 指向现行对象库（照常读取、无需复制），
            #    只镜像正式库的非临时引用——refs/import/* 残留项不得作为
            #    前置提交的可达依据（对象在盘上但无持久引用背书也算缺失）。
            snapshot0 = list_branches(repo)
            q = os.path.join(work, "q.git")
            git(None, "init", "-q", "--bare", q)
            info_dir = os.path.join(q, "objects", "info")
            os.makedirs(info_dir, exist_ok=True)
            with open(os.path.join(info_dir, "alternates"), "w") as fh:
                fh.write(os.path.join(os.path.abspath(repo), "objects") + "\n")
            for ref, sha in list_persistent_refs(repo).items():
                git(q, "update-ref", ref, sha, check=False)

            # 3) 依前置提交确定验包顺序：前置可达才验，验过即取入隔离库，循环推进。
            #    可达依据 = 正式库非临时引用（已镜像进 q）+ 本批已验包取入的
            #    refs/q/*。注意 git bundle verify 只检查前置对象是否存在于对象库，
            #    并不检查引用可达性——对象在盘上但没有持久引用背书时它仍返回 0，
            #    因此必须先做显式可达性门禁，不能只看 verify 的返回码。
            pending = list(range(len(bundles)))
            order = []
            verify_err = {}
            while pending:
                progressed = False
                for idx in list(pending):
                    b = bundles[idx]
                    miss = missing_prereqs(q, b)
                    if miss:
                        continue
                    proc = git(q, "bundle", "verify", b.path, check=False)
                    if proc.returncode == 0:
                        git(q, "fetch", "--no-tags", "--", b.path,
                            f"+{HEADS_PREFIX}*:refs/q/{idx}/heads/*")
                        pending.remove(idx)
                        order.append(idx)
                        progressed = True
                    else:
                        verify_err[idx] = (proc.stderr or proc.stdout).strip()
                if not progressed:
                    break
            result["order"] = [describe(bundles[i]) for i in order]
            if pending:
                missing = {}
                details = []
                for idx in pending:
                    b = bundles[idx]
                    miss = missing_prereqs(q, b)
                    if miss:
                        missing[b.name] = miss
                    else:
                        details.append(f"{b.name}: {verify_err.get(idx, '校验失败')}")
                result["missing"] = missing or None
                parts = []
                if missing:
                    parts.append("缺少前置提交")
                if details:
                    parts.append("；".join(details))
                result["reason"] = "，".join(parts) + "。整批退回，仓库分支保持原状。"
                return result

            # 4) 同名分支（仓库 + 各包）须能排成同一条快进链，生效后指向链尾
            candidates = {}
            for idx in order:
                for ref, sha in bundles[idx].heads.items():
                    candidates.setdefault(ref[len(HEADS_PREFIX):], set()).add(sha)
            for branch in list(candidates):
                old = snapshot0.get(HEADS_PREFIX + branch)
                if old:
                    candidates[branch].add(old)

            updates = []
            unchanged = []
            for branch in sorted(candidates):
                shas = sorted(candidates[branch])
                pair = None
                for a in range(len(shas)):
                    for b_ in range(a + 1, len(shas)):
                        x, y = shas[a], shas[b_]
                        if not is_ancestor(q, x, y) and not is_ancestor(q, y, x):
                            pair = (x, y)
                            break
                    if pair:
                        break
                if pair:
                    result["reason"] = (
                        f"分支 {branch} 在仓库与各包中的提交无法排成同一条快进链："
                        f"{pair[0][:12]} 与 {pair[1][:12]} 互不包含。"
                        "整批退回，仓库分支保持原状。")
                    return result
                tip = next(s for s in shas
                           if all(is_ancestor(q, t, s) for t in shas))
                old = snapshot0.get(HEADS_PREFIX + branch)
                if old == tip:
                    unchanged.append(branch)
                else:
                    updates.append({"branch": branch, "old": old, "new": tip})

            # 5) 验包期间分支已变 → 整批退回
            snapshot1 = list_branches(repo)
            if snapshot1 != snapshot0:
                changed = sorted(
                    ref[len(HEADS_PREFIX):]
                    for ref in set(snapshot0) | set(snapshot1)
                    if snapshot0.get(ref) != snapshot1.get(ref))
                result["reason"] = ("验包期间仓库分支已变化（"
                                    + "、".join(changed)
                                    + "），整批退回，仓库分支保持原状。")
                return result

            if not updates:
                result["ok"] = True
                result["unchanged"] = unchanged
                return result

            # 6) 全部通过：对象迁入正式库（临时引用空间），再一次性更新分支
            batch = uuid.uuid4().hex[:12]
            git(repo, "fetch", "--no-tags", "--", q,
                f"+refs/q/*:{TMP_REFS_PREFIX}{batch}/*")
            tmp_refs_created = True
            lines = ["start"]
            for u in updates:
                lines.append(f"update {HEADS_PREFIX}{u['branch']} "
                             f"{u['new']} {u['old'] or ZERO_SHA}")
            lines.append("commit")
            proc = git(repo, "update-ref", "--stdin",
                       input_text="\n".join(lines) + "\n", check=False)
            if proc.returncode != 0:
                result["reason"] = (
                    "更新分支失败（分支可能在验包期间被并发修改）："
                    f"{proc.stderr.strip()}。整批退回，仓库分支保持原状。")
                return result

            result["ok"] = True
            result["updates"] = updates
            result["unchanged"] = unchanged
            return result
        except GitError as exc:
            result["reason"] = f"内部 git 操作失败：{exc}。整批退回。"
            return result
        except Exception as exc:  # 兜底：任何意外错误都不应影响正式库
            result["reason"] = f"内部错误：{exc!r}。整批退回。"
            return result
        finally:
            # 只清理本批自己的临时引用空间，不碰其他批次的历史残留（那是启动
            # 清理的职责）；逐个删除，避免单个失败连累本批其余引用也清不掉。
            # 只删引用，不扫描、不复制、不删除任何对象。
            if tmp_refs_created and batch is not None:
                own_prefix = f"{TMP_REFS_PREFIX}{batch}/"
                own_refs = sorted(list_refs(repo, own_prefix))
                failed = delete_refs_individually(repo, own_refs)
                if failed:
                    logger.error("本批临时引用有 %d 项删除失败（共 %d 项），"
                                 "将在下次启动时重试清理：%s",
                                 len(failed), len(own_refs), failed[0][1])
            shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------
# 网页
# --------------------------------------------------------------------------

def page_context(result=None):
    # 重启后首次打开页面即清理上次强停遗留的临时引用（仅一次，失败不阻断）
    ensure_startup_cleanup(REPO_PATH)
    try:
        branches = list_branches(REPO_PATH)
        repo_error = None
    except Exception as exc:  # 仓库尚未初始化等
        branches, repo_error = {}, str(exc)
    return {
        "repo": REPO_PATH,
        "branches": branches,
        "repo_error": repo_error,
        "samples": list_sample_files(SAMPLES_DIR),
        "bad_samples": list_sample_files(SAMPLES_BAD_DIR),
        "max_bundles": MAX_BUNDLES,
        "result": result,
    }


def list_sample_files(directory):
    if directory and os.path.isdir(directory):
        return sorted(f for f in os.listdir(directory) if f.endswith(".bundle"))
    return []


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", page_context())


@app.post("/import", response_class=HTMLResponse)
async def import_upload(request: Request):
    form = await request.form()
    files = []
    for item in form.getlist("files"):
        if isinstance(item, UploadFile) and item.filename:
            files.append((item.filename, await item.read()))
    result = await run_in_threadpool(import_bundles, REPO_PATH, files)
    return templates.TemplateResponse(request, "index.html", page_context(result))


@app.post("/import-samples", response_class=HTMLResponse)
async def import_samples(request: Request):
    form = await request.form()
    files = []
    for name in form.getlist("sample"):
        base = os.path.basename(name)
        for directory in (SAMPLES_DIR, SAMPLES_BAD_DIR):
            path = os.path.join(directory, base) if directory else ""
            if path and os.path.isfile(path):
                with open(path, "rb") as fh:
                    files.append((base, fh.read()))
                break
    result = await run_in_threadpool(import_bundles, REPO_PATH, files)
    return templates.TemplateResponse(request, "index.html", page_context(result))
