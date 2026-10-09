# -*- coding: utf-8 -*-
"""
新媒体工作流执行器 · 本地服务
------------------------------------------------------------
作用：
  1. 给网页提供一个"能碰硬盘"的后端（浏览器自己做不到）
  2. 代理大模型请求（顺便绕开 CORS，换成哪家模型都不用改网页）
  3. 调 ffmpeg 自动出片
  4. 生成剪映草稿并注册到剪映索引

启动方式：双击同目录下的「启动工作流.bat」
不依赖任何需要额外安装的东西（用的都是 Python 自带库 + 已装的三个包）
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ---------------------------------------------------------------- 基础路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(BASE_DIR, "新媒体工作流执行器.html")
OUT_DIR = os.path.join(BASE_DIR, "导出成片")
CACHE_DIR = os.path.join(BASE_DIR, ".缓存")
IMAGE_DIR = os.path.join(BASE_DIR, "新媒体", "交付", "素材切图")
JY_DRAFT_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "JianyingPro", "User Data", "Projects", "com.lveditor.draft",
)

PORT = 8765
HOST = "127.0.0.1"


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- ffmpeg
_FFMPEG = None


def ffmpeg_exe():
    """拿到 ffmpeg 可执行文件路径。优先用 imageio-ffmpeg 自带的，其次找系统 PATH。"""
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    try:
        import imageio_ffmpeg
        _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
        return _FFMPEG
    except Exception:
        pass
    for name in ("ffmpeg", "ffmpeg.exe"):
        p = shutil.which(name)
        if p:
            _FFMPEG = p
            return p
    return None


# ---------------------------------------------------------------- 素材
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".avi", ".mkv")


def list_assets():
    """列出可用素材（文件名 + 类型 + 绝对路径）"""
    out = []
    for root, _dirs, files in os.walk(os.path.join(BASE_DIR, "新媒体")):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in IMAGE_EXT or ext in VIDEO_EXT:
                full = os.path.join(root, f)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                out.append({
                    "name": f,
                    "path": full,
                    "kind": "video" if ext in VIDEO_EXT else "image",
                    "dir": os.path.basename(root),
                    "size": size,
                })
    return out


def find_asset(fragment):
    """按关键词模糊匹配素材（网页传过来的可能是简称，比如 '习04'）"""
    if not fragment:
        return None
    frag = str(fragment).strip()
    # 绝对路径直接返回
    if os.path.isabs(frag) and os.path.exists(frag):
        return frag
    assets = list_assets()
    # 1) 文件名完全相等
    for a in assets:
        if a["name"] == frag:
            return a["path"]
    # 2) 文件名包含
    cands = [a for a in assets if frag in a["name"]]
    if not cands:
        # 3) 去掉下划线/空格再比
        key = re.sub(r"[\s_\-]", "", frag)
        cands = [a for a in assets if key and key in re.sub(r"[\s_\-]", "", a["name"])]
    if not cands:
        return None
    # 图片优先
    cands.sort(key=lambda a: (a["kind"] != "image", a["name"]))
    return cands[0]["path"]


def probe_duration(path):
    """取视频时长（秒）"""
    exe = ffmpeg_exe()
    if not exe:
        return 0.0
    try:
        r = subprocess.run([exe, "-hide_banner", "-i", path],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr or "")
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mi * 60 + s
    except Exception:
        pass
    return 0.0


# ---------------------------------------------------------------- 字幕
def build_srt(subs):
    """subs: [{'start':0.2,'end':2.8,'text':'...'}, ...]"""
    lines = []
    for i, s in enumerate(subs, 1):
        st = max(0.0, float(s.get("start", 0)))
        en = max(st + 0.3, float(s.get("end", st + 2)))
        txt = str(s.get("text", "")).strip().replace("\n", " ")
        lines.append(str(i))
        lines.append("%s --> %s" % (fmt_ts(st), fmt_ts(en)))
        lines.append(txt)
        lines.append("")
    return "\n".join(lines)


def fmt_ts(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms == 1000:
        ms = 999
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def write_srt(subs, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(build_srt(subs))
    return path


# ---------------------------------------------------------------- 渲染
MOTIONS = ("zoom-in", "zoom-out", "pan-left", "pan-right", "still")


def build_motion_filter(motion, total_frames, w, h, fps):
    """
    生成 Ken Burns 运镜滤镜片段。

    关键：d 必须固定为 1。
    输入用 -loop 1 时本身已经产生 total_frames 帧，若再给 d=total_frames，
    zoompan 会为「每一帧」都生成 total_frames 帧 —— 结果是 total_frames² 帧。
    实测：3 秒镜头会渲成 4 分 30 秒、67MB。
    正确做法是 d=1，让输入帧数决定总帧数，用 on（输出帧序号）驱动缩放。
    """
    step = 0.0022  # 每帧缩放增量，90 帧约推近 20%
    if motion == "zoom-out":
        z = "max(1.20-%.4f*on,1.0)" % step
    elif motion == "still":
        z = "1.0"
    else:  # zoom-in
        z = "min(1.0+%.4f*on,1.20)" % step

    if motion == "pan-left":
        x, y = "(iw-iw/zoom)*(1-on/%d)" % max(1, total_frames), "(ih-ih/zoom)/2"
    elif motion == "pan-right":
        x, y = "(iw-iw/zoom)*(on/%d)" % max(1, total_frames), "(ih-ih/zoom)/2"
    else:
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"

    return ("zoompan=z='%s':x='%s':y='%s':d=1:s=%dx%d:fps=%d"
            % (z, x, y, w, h, fps))


def render(spec, progress=None):
    """
    spec = {
      width, height, fps, transition, bgm,
      shots: [ {asset, duration, motion, subtitle} ]
    }
    返回 {ok, out, message}
    """
    def pg(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    exe = ffmpeg_exe()
    if not exe:
        return {"ok": False, "message": "找不到 ffmpeg。请先运行一次「启动工作流.bat」，它会自动装好。"}

    shots = spec.get("shots") or []
    if not shots:
        return {"ok": False, "message": "分镜是空的，没有可剪的内容。"}

    w = int(spec.get("width") or 1080)
    h = int(spec.get("height") or 1920)
    fps = int(spec.get("fps") or 30)
    trans = float(spec.get("transition", 0.4))

    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    work = os.path.join(CACHE_DIR, "render_" + stamp)
    os.makedirs(work, exist_ok=True)

    # 1) 逐个镜头生成中间片段
    clips = []
    for i, sh in enumerate(shots):
        path = find_asset(sh.get("asset"))
        dur = float(sh.get("duration") or 3)
        if dur <= 0:
            dur = 3
        if not path:
            pg("第 %d 个镜头找不到素材「%s」，已用占位色块代替" % (i + 1, sh.get("asset")))
            clip = os.path.join(work, "c%02d.mp4" % i)
            subprocess.run([exe, "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", "color=c=0x1f2937:s=%dx%d:d=%.2f" % (w, h, dur),
                            "-vf", "format=yuv420p", "-r", str(fps), "-c:v", "libx264", clip],
                           capture_output=True)
            clips.append({"file": clip, "dur": dur})
            continue

        ext = os.path.splitext(path)[1].lower()
        clip = os.path.join(work, "c%02d.mp4" % i)
        motion = sh.get("motion") or "zoom-in"
        if motion not in MOTIONS:
            motion = "zoom-in"

        if ext in VIDEO_EXT:
            src_dur = probe_duration(path) or dur
            start = float(sh.get("source_start") or 0)
            avail = max(0.5, src_dur - start)
            use = min(dur, avail)
            vf = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1,fps=%d"
                  % (w, h, w, h, fps))
            cmd = [exe, "-y", "-hide_banner", "-loglevel", "error",
                   "-ss", "%.3f" % start, "-t", "%.3f" % use, "-i", path,
                   "-vf", vf, "-an", "-r", str(fps),
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                   "-pix_fmt", "yuv420p", clip]
            subprocess.run(cmd, capture_output=True)
            if not os.path.exists(clip) or os.path.getsize(clip) < 1000:
                # 视频读取失败 → 退化成色块
                subprocess.run([exe, "-y", "-hide_banner", "-loglevel", "error",
                                "-f", "lavfi", "-i", "color=c=0x1f2937:s=%dx%d:d=%.2f" % (w, h, dur),
                                "-vf", "format=yuv420p", "-r", str(fps), "-c:v", "libx264", clip],
                               capture_output=True)
                use = dur
            clips.append({"file": clip, "dur": use})
        else:
            frames = max(1, int(round(dur * fps)))
            vf = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
                  "%s,setsar=1,format=yuv420p"
                  % (w, h, w, h, build_motion_filter(motion, frames, w, h, fps)))
            cmd = [exe, "-y", "-hide_banner", "-loglevel", "error",
                   "-loop", "1", "-framerate", str(fps), "-t", "%.3f" % dur, "-i", path,
                   "-vf", vf, "-frames:v", str(frames), "-r", str(fps),
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                   "-pix_fmt", "yuv420p", clip]
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0 or not os.path.exists(clip) or os.path.getsize(clip) < 1000:
                pg("第 %d 个镜头渲染失败，已用占位色块代替" % (i + 1))
                subprocess.run([exe, "-y", "-hide_banner", "-loglevel", "error",
                                "-f", "lavfi", "-i", "color=c=0x1f2937:s=%dx%d:d=%.2f" % (w, h, dur),
                                "-vf", "format=yuv420p", "-r", str(fps),
                                "-c:v", "libx264", "-preset", "veryfast", clip],
                               capture_output=True)
            clips.append({"file": clip, "dur": dur})

        pg("镜头 %d/%d 完成" % (i + 1, len(shots)))

    clips = [c for c in clips if os.path.exists(c["file"]) and os.path.getsize(c["file"]) > 500]
    if not clips:
        return {"ok": False, "message": "所有镜头都渲染失败了，请检查素材文件是否还在。"}

    # 2) 拼接（有转场就用 xfade，否则直接 concat）
    pg("正在拼接和加转场…")
    silent = os.path.join(work, "silent.mp4")
    joined = os.path.join(work, "joined.mp4")

    if len(clips) == 1:
        shutil.copy2(clips[0]["file"], joined)
        total = clips[0]["dur"]
    else:
        # 单趟 xfade：所有片段一次性喂进去，链式转场，只编码一次。
        # （逐级 xfade 是 O(n²) 编码量，8 个镜头会慢到不可用）
        parts = []
        for i in range(len(clips)):
            parts.append("[%d:v]settb=AVTB,fps=%d,format=yuv420p[in%02d]" % (i, fps, i))
        cur = "[in00]"
        prev_dur = clips[0]["dur"]
        for i in range(1, len(clips)):
            out = ("[v%02d]" % i) if i < len(clips) - 1 else "[vout]"
            off = max(0.0, prev_dur - trans)
            parts.append("%s[in%02d]xfade=transition=fade:duration=%.3f:offset=%.3f%s"
                         % (cur, i, trans, off, out))
            prev_dur = prev_dur + clips[i]["dur"] - trans
            cur = out
        total = prev_dur

        cmd = [exe, "-y", "-hide_banner", "-loglevel", "error"]
        for c in clips:
            cmd += ["-i", c["file"]]
        cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]",
                "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                "-pix_fmt", "yuv420p", joined]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 or not os.path.exists(joined) or os.path.getsize(joined) < 1000:
            # 退化成不带转场的硬切拼接
            pg("转场这一步没成功，改为硬切拼接")
            lst = os.path.join(work, "list.txt")
            with open(lst, "w", encoding="utf-8") as f:
                for c in clips:
                    f.write("file '%s'\n" % c["file"].replace("\\", "/"))
            subprocess.run([exe, "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "concat", "-safe", "0", "-i", lst,
                            "-c", "copy", joined], capture_output=True)
            total = sum(c["dur"] for c in clips)

    # 3) 烧字幕
    subs = spec.get("subtitles") or []
    # 只过滤标题，别过滤整个路径（否则 C: 和反斜杠会被一起替换掉）
    safe_title = re.sub(r'[\\/:*?"<>|]', "_", str(spec.get("title") or "成片")).strip()[:40] or "成片"
    final = os.path.join(OUT_DIR, "%s_%s.mp4" % (safe_title, stamp))

    if subs:
        pg("正在烧字幕…")
        srt = write_srt(subs, os.path.join(work, "sub.srt"))
        srt_esc = srt.replace("\\", "/").replace(":", "\\:")
        # ⚠️ 坑：必须显式声明 PlayResX/PlayResY。
        # libass 处理 SRT 时默认虚拟画布只有 288 像素高，此时 MarginV=300 会比画布还大，
        # 字幕被顶到画面外 —— 表现是「命令成功、字幕消失」，极难排查。
        # 对齐真实分辨率后，MarginV / FontSize 才是真实像素，行为可预期。
        style = ("PlayResX=%d,PlayResY=%d,FontName=Microsoft YaHei,FontSize=%d,"
                 "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                 "BorderStyle=1,Outline=5,Shadow=1,Alignment=2,MarginV=%d"
                 % (w, h, int(spec.get("fontSize") or 90), int(h * 0.16)))
        cmd = [exe, "-y", "-hide_banner", "-loglevel", "error", "-i", joined,
               "-vf", "subtitles='%s':force_style='%s'" % (srt_esc, style),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
               "-pix_fmt", "yuv420p", "-r", str(fps),
               "-an", silent]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 or not os.path.exists(silent) or os.path.getsize(silent) < 1000:
            pg("字幕烧录失败，输出无字幕成片")
            silent = joined
    else:
        silent = joined

    # 4) 配音轨
    bgm = find_asset(spec.get("bgm")) if spec.get("bgm") else None
    if bgm:
        pg("正在混音…")
        cmd = [exe, "-y", "-hide_banner", "-loglevel", "error",
               "-i", silent, "-stream_loop", "-1", "-i", bgm,
               "-filter_complex",
               "[1:a]volume=%.2f,afade=t=out:st=%.2f:d=1.5[a]" % (float(spec.get("bgmVolume", 0.25)), max(0, total - 1.5)),
               "-map", "0:v", "-map", "[a]", "-shortest",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", final]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 or not os.path.exists(final) or os.path.getsize(final) < 1000:
            shutil.copy2(silent, final)
    else:
        # 没有 BGM 也补一条静音轨：部分平台对「无音轨视频」会异常
        pg("正在补静音音轨…")
        cmd = [exe, "-y", "-hide_banner", "-loglevel", "error",
               "-i", silent, "-f", "lavfi", "-t", "%.3f" % max(0.5, total),
               "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
               "-map", "0:v", "-map", "1:a", "-shortest",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", final]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 or not os.path.exists(final) or os.path.getsize(final) < 1000:
            shutil.copy2(silent, final)

    if not os.path.exists(final) or os.path.getsize(final) < 1000:
        return {"ok": False, "message": "渲染失败，没有生成有效的视频文件。"}

    # 清理中间文件
    try:
        shutil.rmtree(work, ignore_errors=True)
    except Exception:
        pass

    return {"ok": True, "out": final, "size": os.path.getsize(final),
            "duration": round(total, 2), "shots": len(clips)}


# ---------------------------------------------------------------- 剪映草稿
def make_jianying(spec, progress=None):
    """生成剪映草稿并注册到索引"""
    def pg(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    try:
        import pyJianYingDraft as d
        from pyJianYingDraft.track import TrackSpec
    except Exception:
        return {"ok": False, "message": "缺少剪映草稿库。请关掉窗口重新双击「启动工作流.bat」，它会自动装好。"}

    shots = spec.get("shots") or []
    if not shots:
        return {"ok": False, "message": "分镜是空的。"}
    if not os.path.isdir(JY_DRAFT_DIR):
        return {"ok": False, "message": "找不到剪映草稿目录。请确认已安装剪映专业版并至少打开过一次。"}

    w = int(spec.get("width") or 1080)
    h = int(spec.get("height") or 1920)
    fps = int(spec.get("fps") or 30)
    name = re.sub(r'[\\/:*?"<>|]', "_", str(spec.get("title") or "工作流草稿"))
    name = ("【自动】" + name)[:40]

    full = os.path.join(JY_DRAFT_DIR, name)
    if os.path.isdir(full):
        shutil.rmtree(full, ignore_errors=True)

    pg("正在生成剪映草稿…")
    try:
        folder = d.DraftFolder(JY_DRAFT_DIR)
        script = folder.create_draft(name, w, h, fps=fps)
        script.append_track(TrackSpec(d.TrackType.video, name="主画面"))

        t = 0.0
        for sh in shots:
            path = find_asset(sh.get("asset"))
            dur = float(sh.get("duration") or 3)
            if not path:
                t += dur
                continue
            mat = d.VideoMaterial(path)
            src = None
            if os.path.splitext(path)[1].lower() in VIDEO_EXT:
                sd = probe_duration(path)
                if sd > dur:
                    src = d.trange("%.3fs" % float(sh.get("source_start") or 0), "%.3fs" % dur)
            seg = d.VideoSegment(mat, d.trange("%.3fs" % t, "%.3fs" % dur), source_timerange=src)
            script.add_segment(seg, "主画面")
            t += dur

        subs = spec.get("subtitles") or []
        if subs:
            pg("正在写入字幕…")
            script.append_track(TrackSpec(d.TrackType.text, name="字幕"))
            style = d.TextStyle(size=9.0, bold=True, color=(1.0, 1.0, 1.0), align=1)
            border = d.TextBorder(color=(0.0, 0.0, 0.0), width=40.0)
            for s in subs:
                txt = str(s.get("text", "")).strip()
                if not txt:
                    continue
                seg = d.TextSegment(txt, d.trange("%.3fs" % float(s.get("start", 0)),
                                                  "%.3fs" % max(0.5, float(s.get("end", 2)) - float(s.get("start", 0)))),
                                    style=style, border=border,
                                    clip_settings=d.ClipSettings(transform_y=-0.72))
                script.add_segment(seg, "字幕")
        script.save()
    except Exception as e:
        return {"ok": False, "message": "生成剪映草稿失败：%s" % e}

    # 注册到剪映索引
    pg("正在注册到剪映索引…")
    reg_warn = ""
    try:
        root = os.path.join(JY_DRAFT_DIR, "root_meta_info.json")
        if os.path.exists(root):
            with open(root, "r", encoding="utf-8") as f:
                j = json.load(f)
            store = j.get("all_draft_store", [])
            if not any(e.get("draft_name") == name for e in store):
                now = int(time.time() * 1e6)
                fold = full.replace("/", "\\")
                store.insert(0, {
                    "cloud_draft_cover": False, "cloud_draft_sync": False,
                    "draft_cloud_last_action_download": False,
                    "draft_cloud_purchase_info": "", "draft_cloud_template_id": "",
                    "draft_cloud_tutorial_info": "", "draft_cloud_videocut_purchase_info": "",
                    "draft_cover": "", "draft_fold_path": fold,
                    "draft_id": str(uuid.uuid4()).upper(),
                    "draft_is_ai_shorts": False, "draft_is_cloud_temp_draft": False,
                    "draft_is_infinite_canvas_draft": False, "draft_is_invisible": False,
                    "draft_is_pippit_draft": False, "draft_is_web_article_video": False,
                    "draft_json_file": fold + "\\draft_content.json",
                    "draft_name": name, "draft_new_version": "",
                    "draft_root_path": JY_DRAFT_DIR,
                    "draft_timeline_materials_size": 0, "draft_type": "",
                    "draft_web_article_video_enter_from": "",
                    "pippit_avatar_url": "", "pippit_extra_info": "", "pippit_id": "",
                    "pippit_user_name": "", "streaming_edit_draft_ready": True,
                    "tm_draft_cloud_completed": "", "tm_draft_cloud_entry_id": -1,
                    "tm_draft_cloud_modified": 0, "tm_draft_cloud_parent_entry_id": -1,
                    "tm_draft_cloud_space_id": -1, "tm_draft_cloud_user_id": -1,
                    "tm_draft_create": now, "tm_draft_modified": now, "tm_draft_removed": 0,
                    "tm_duration": int(t * 1e6),
                })
                j["all_draft_store"] = store
                if isinstance(j.get("draft_ids"), int):
                    j["draft_ids"] += 1
                # 改动前备份一次
                bak = root + ".bak_workflow"
                if not os.path.exists(bak):
                    shutil.copy2(root, bak)
                with open(root, "w", encoding="utf-8") as f:
                    json.dump(j, f, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        reg_warn = "（草稿已生成，但注册索引失败：%s。可以退出剪映重进，剪映自己会扫描到）" % e

    return {"ok": True, "draft": full, "name": name, "warn": reg_warn,
            "message": "剪映草稿已生成：%s%s" % (name, reg_warn)}


# ---------------------------------------------------------------- 大模型代理
def proxy_chat(payload, write_chunk):
    """
    payload = { baseUrl, apiKey, model, messages, temperature }
    把上游的流式响应原样转发给网页（网页因此不受 CORS 限制）
    """
    base = (payload.get("baseUrl") or "https://api.deepseek.com").rstrip("/")
    if base.endswith("/chat/completions"):
        url = base
    elif base.endswith("/v1"):
        url = base + "/chat/completions"
    else:
        url = base + "/v1/chat/completions"
    # DeepSeek 官方不用 /v1
    if "api.deepseek.com" in base and "/chat/completions" not in base:
        url = base + "/chat/completions"

    body = json.dumps({
        "model": payload.get("model") or "deepseek-flash",
        "messages": payload.get("messages") or [],
        "stream": True,
        "temperature": float(payload.get("temperature", 1.0)),
    }).encode("utf-8")

    req = Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + str(payload.get("apiKey") or ""),
        "Accept": "text/event-stream",
    })

    try:
        resp = urlopen(req, timeout=180)
    except HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        msg = ""
        try:
            msg = json.loads(raw).get("error", {}).get("message", "")
        except Exception:
            msg = raw[:200]
        code = e.code
        if code in (401, 403):
            friendly = "API Key 无效，请检查是否复制完整"
        elif code == 402:
            friendly = "账户余额不足，请到模型平台充值"
        elif code == 429:
            friendly = "请求太频繁了，请等一分钟再试"
        elif code >= 500:
            friendly = "模型服务暂时不可用，请稍后重试"
        elif code == 404 or (code == 400 and "model" in msg.lower()):
            friendly = "MODEL_UNSUPPORTED"
        else:
            friendly = "请求被拒绝（%d），请检查模型名称和接口地址" % code
        write_chunk(("__ERR__" + friendly).encode("utf-8"))
        return
    except URLError as e:
        write_chunk(b"__ERR__\xe7\xbd\x91\xe7\xbb\x9c\xe8\xbf\x9e\xe6\x8e\xa5\xe5\xa4\xb1\xe8\xb4\xa5\xef\xbc\x8c\xe8\xaf\xb7\xe6\xa3\x80\xe6\x9f\xa5\xe7\xbd\x91\xe7\xbb\x9c\xe5\x90\x8e\xe9\x87\x8d\xe8\xaf\x95")
        return
    except Exception as e:
        write_chunk(("__ERR__请求失败：" + str(e)).encode("utf-8"))
        return

    while True:
        chunk = resp.read(1024)
        if not chunk:
            break
        write_chunk(chunk)


# ---------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 静音，别刷屏

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            if not os.path.exists(HTML_FILE):
                self._json({"ok": False, "message": "找不到网页文件：%s" % HTML_FILE}, 404)
                return
            data = open(HTML_FILE, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/api/health":
            self._json({
                "ok": True,
                "ffmpeg": bool(ffmpeg_exe()),
                "jianying": os.path.isdir(JY_DRAFT_DIR),
                "assets": len(list_assets()),
                "outdir": OUT_DIR,
            })
            return
        if p == "/api/assets":
            self._json({"ok": True, "assets": list_assets()})
            return
        self._json({"ok": False, "message": "没有这个地址"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path
        body = self._read_body()

        if p == "/api/chat":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self._cors()
            self.end_headers()

            def w(b):
                try:
                    self.wfile.write(b)
                    self.wfile.flush()
                except Exception:
                    pass
            proxy_chat(body, w)
            try:
                self.wfile.write(b"\n\n__DONE__")
                self.wfile.flush()
            except Exception:
                pass
            return

        if p == "/api/render":
            try:
                r = render(body)
            except Exception as e:
                r = {"ok": False, "message": "出片时出错：%s" % e}
            self._json(r)
            return

        if p == "/api/jianying":
            try:
                r = make_jianying(body)
            except Exception as e:
                r = {"ok": False, "message": "生成草稿时出错：%s" % e}
            self._json(r)
            return

        if p == "/api/open":
            # 在资源管理器里定位文件
            t = body.get("path") or ""
            try:
                if os.path.exists(t):
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(t)])
                    self._json({"ok": True})
                else:
                    self._json({"ok": False, "message": "文件不存在"})
            except Exception as e:
                self._json({"ok": False, "message": str(e)})
            return

        self._json({"ok": False, "message": "没有这个接口"}, 404)


def pick_port(start=PORT):
    import socket
    for p in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((HOST, p)) != 0:
                return p
    return start


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    global PORT
    PORT = pick_port(PORT)

    log("=" * 58)
    log("  新媒体工作流执行器 · 本地服务")
    log("=" * 58)
    fx = ffmpeg_exe()
    log("  自动剪辑(ffmpeg) :", "就绪" if fx else "缺失（文案功能仍可用）")
    log("  剪映草稿         :", "就绪" if os.path.isdir(JY_DRAFT_DIR) else "未检测到剪映")
    log("  可用素材         :", "%d 个文件" % len(list_assets()))
    log("  成片输出目录     :", OUT_DIR)
    log("-" * 58)
    log("  网页地址: http://%s:%d" % (HOST, PORT))
    log("  用完直接关掉这个黑窗口即可。")
    log("=" * 58)

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    if not os.environ.get("WF_NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open("http://%s:%d" % (HOST, PORT))).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("\n已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
