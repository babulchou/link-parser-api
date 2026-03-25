"""
链接内容解析 API
===============
复用小红书 MCP 的 httpx 服务端抓取能力，扩展支持多平台链接解析。
为随手记 Web App 提供 HTTP API 接口。

支持平台：小红书、YouTube、B站、知乎、微信公众号、掘金、少数派、36氪、GitHub、通用网页
"""

import json
import logging
import re
import os
import asyncio
from urllib.parse import quote, urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

# ── 小红书 API 客户端（通过 xiaohongshu-cli）────────────────────
_xhs_client = None

def _get_xhs_client():
    """获取或初始化 XhsClient 单例。从环境变量 XHS_COOKIES 读取 Cookie JSON。"""
    global _xhs_client
    if _xhs_client is not None:
        return _xhs_client

    cookies_json = os.environ.get("XHS_COOKIES", "")
    if not cookies_json:
        logger.warning("XHS_COOKIES 环境变量未设置，小红书 API 解析不可用")
        return None

    try:
        from xhs_cli.client import XhsClient
        cookies = json.loads(cookies_json)
        _xhs_client = XhsClient(cookies=cookies, request_delay=0.5, max_retries=2)
        logger.info("XhsClient 初始化成功")
        return _xhs_client
    except Exception as e:
        logger.error("XhsClient 初始化失败: %s", e)
        return None

app = FastAPI(title="Link Parser API", version="1.0.0")

# 允许所有来源（随手记前端是 GitHub Pages）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── 通用请求头 ──────────────────────────────────────────────────
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# ── 平台识别 ────────────────────────────────────────────────────
def identify_platform(url: str) -> dict:
    u = url.lower()
    if any(d in u for d in ["xiaohongshu.com", "xhslink.com", "xhs.cn"]):
        return {"id": "xiaohongshu", "name": "小红书", "icon": "📕"}
    if any(d in u for d in ["youtube.com", "youtu.be"]):
        return {"id": "youtube", "name": "YouTube", "icon": "▶️"}
    if any(d in u for d in ["bilibili.com", "b23.tv"]):
        return {"id": "bilibili", "name": "B站", "icon": "📺"}
    if "zhihu.com" in u:
        return {"id": "zhihu", "name": "知乎", "icon": "💬"}
    if any(d in u for d in ["weixin.qq.com", "mp.weixin"]):
        return {"id": "wechat", "name": "微信公众号", "icon": "💚"}
    if "douyin.com" in u:
        return {"id": "douyin", "name": "抖音", "icon": "🎵"}
    if any(d in u for d in ["twitter.com", "x.com"]):
        return {"id": "twitter", "name": "X/Twitter", "icon": "🐦"}
    if "github.com" in u:
        return {"id": "github", "name": "GitHub", "icon": "🐙"}
    if "juejin.cn" in u:
        return {"id": "juejin", "name": "掘金", "icon": "💎"}
    if "36kr.com" in u:
        return {"id": "36kr", "name": "36氪", "icon": "📰"}
    if "sspai.com" in u:
        return {"id": "sspai", "name": "少数派", "icon": "📝"}
    if any(d in u for d in ["notion.so", "notion.site"]):
        return {"id": "notion", "name": "Notion", "icon": "📄"}
    if "medium.com" in u:
        return {"id": "medium", "name": "Medium", "icon": "📖"}
    try:
        host = urlparse(url).hostname or ""
        return {"id": "web", "name": host.replace("www.", ""), "icon": "🔗"}
    except Exception:
        return {"id": "web", "name": "网页", "icon": "🔗"}


# ═══════════════════════════════════════════════════════════════════
#  小红书解析（方案 A: xhs CLI → 方案 B: xhs_cli API → 方案 C: HTML）
# ═══════════════════════════════════════════════════════════════════

import subprocess
import shutil

def _extract_xhs_note_id(url: str) -> str | None:
    """从小红书链接提取笔记 ID"""
    patterns = [
        r'xiaohongshu\.com/(?:explore|discovery/item)/([a-zA-Z0-9]+)',
        r'xhslink\.com/(?:a/)?([a-zA-Z0-9/]+)',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def _format_xhs_count(count) -> str:
    """格式化小红书互动数"""
    if isinstance(count, str):
        count = count.replace(",", "")
        try:
            count = int(count)
        except ValueError:
            return str(count)
    if isinstance(count, (int, float)):
        if count >= 10000:
            return f"{count/10000:.1f}万"
        return str(int(count))
    return str(count) if count else "0"


def _extract_image_urls(image_list: list) -> list[str]:
    """从 image_list 提取高清图片 URL"""
    urls = []
    for img in image_list:
        url = img.get("url_default", "")
        if not url:
            for info in img.get("info_list", []):
                if info.get("image_scene") == "WB_DFT":
                    url = info.get("url", "")
                    break
        if not url:
            url = img.get("url_pre", "") or img.get("url", "")
        if url:
            urls.append(url)
    return urls


def _run_xhs_cli(args: list[str], timeout: int = 25) -> dict | None:
    """执行 xhs CLI 命令，返回 JSON 数据"""
    xhs_path = shutil.which("xhs")
    if not xhs_path:
        return None
    cmd = [xhs_path] + args + ["--json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if data.get("ok"):
            return data.get("data", {})
        return None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def _format_xhs_result_from_note_card(note_card: dict) -> dict | None:
    """从 xhs CLI 的 note_card 数据格式化为 API 返回结果"""
    title = note_card.get("title", "") or note_card.get("display_title", "")
    desc = note_card.get("desc", "")
    user = note_card.get("user", {})
    author = user.get("nickname", "") or user.get("nick_name", "")

    if not title and not desc and not author:
        return None

    interact = note_card.get("interact_info", {})
    likes = interact.get("liked_count", "0")
    comments = interact.get("comment_count", "0")
    collects = interact.get("collected_count", "0")
    shares = interact.get("share_count", "0")

    image_list = note_card.get("image_list", [])
    images = _extract_image_urls(image_list)

    tags = []
    for tag in note_card.get("tag_list", []):
        tag_name = tag.get("name", "") if isinstance(tag, dict) else str(tag)
        if tag_name:
            tags.append(tag_name)

    note_type = "视频" if note_card.get("type") == "video" else "图文"

    # 清理正文中的话题标签语法
    clean_desc = re.sub(r'#[^#]+?\[话题\]#\s*', '', desc).strip() if desc else ""
    summary_text = clean_desc if clean_desc else title
    if len(summary_text) > 300:
        summary_text = summary_text[:300] + "..."

    result = {
        "title": title or "小红书笔记",
        "summary": summary_text,
        "author": author or "未知",
        "likes": _format_xhs_count(likes),
        "comments": _format_xhs_count(comments),
        "collects": _format_xhs_count(collects),
        "shares": _format_xhs_count(shares),
        "tags": tags,
        "content_type": note_type,
        "images": images,
        "image_count": len(images),
        "extra_info": (
            f"👤 {author or '未知'} · "
            f"❤️ {_format_xhs_count(likes)} · "
            f"💬 {_format_xhs_count(comments)} · "
            f"⭐ {_format_xhs_count(collects)}"
        ),
    }
    if images:
        result["cover_image"] = images[0]
    return result


async def _resolve_xhs_short_link(url: str, client: httpx.AsyncClient) -> str | None:
    """解析小红书短链接，返回最终笔记 ID"""
    try:
        resp = await client.get(url, follow_redirects=True)
        final_url = str(resp.url)
        return _extract_xhs_note_id(final_url)
    except Exception:
        return None


async def parse_xiaohongshu(url: str, client: httpx.AsyncClient) -> dict:
    """解析小红书笔记内容 — 三层降级：xhs CLI → xhs_cli API → HTML 解析"""
    note_id = _extract_xhs_note_id(url)
    logger.info("小红书解析开始: url=%s, 初始note_id=%s", url, note_id)

    # 如果是短链接，先跟随重定向拿到笔记 ID
    if "xhslink.com" in url or not note_id:
        note_id = await _resolve_xhs_short_link(url, client)
        logger.info("短链接重定向后 note_id=%s", note_id)

    if not note_id:
        logger.warning("无法提取笔记ID: url=%s", url)
        return {"error": "无法从链接提取笔记ID"}

    # ── 方案 A: 使用 xhs CLI subprocess（本地有浏览器 Cookie 时可用）──
    cli_data = _run_xhs_cli(["read", note_id], timeout=25)
    if cli_data:
        items = cli_data.get("items", [])
        if items:
            note_card = items[0].get("note_card", {})
            if note_card:
                result = _format_xhs_result_from_note_card(note_card)
                if result:
                    logger.info("方案A(xhs CLI)成功: note_id=%s", note_id)
                    return result
        logger.info("方案A(xhs CLI): 数据为空, items=%d", len(cli_data.get("items", [])))
    else:
        xhs_path = shutil.which("xhs")
        logger.info("方案A(xhs CLI)跳过: xhs_path=%s, cli_data=%s", xhs_path, cli_data)

    # ── 方案 B: 使用 xhs_cli Python API（Render 等环境用 Cookie 环境变量）──
    xhs = _get_xhs_client()
    if xhs:
        try:
            detail = xhs.get_note_detail(note_id)
            logger.info("方案B(xhs_cli API): detail=%s", "有数据" if detail else "空")
            if detail:
                title = detail.get("title", "") or detail.get("displayTitle", "")
                desc = detail.get("desc", "")
                user = detail.get("user", {})
                author = user.get("nickname", "") or user.get("nick_name", "")

                interact = detail.get("interactInfo", detail.get("interact_info", {}))
                likes = interact.get("likedCount", interact.get("liked_count", "0"))
                comments_cnt = interact.get("commentCount", interact.get("comment_count", "0"))
                collects = interact.get("collectedCount", interact.get("collected_count", "0"))

                image_list = detail.get("imageList", detail.get("image_list", []))
                images = _extract_image_urls(image_list)

                tags = []
                for tag in detail.get("tagList", detail.get("tag_list", [])):
                    tag_name = tag.get("name", "") if isinstance(tag, dict) else str(tag)
                    if tag_name:
                        tags.append(tag_name)

                note_type = "视频" if detail.get("type", "normal") == "video" else "图文"

                if title or desc or author:
                    summary_text = desc if desc else title
                    if len(summary_text) > 300:
                        summary_text = summary_text[:300] + "..."

                    result = {
                        "title": title or "小红书笔记",
                        "summary": summary_text,
                        "author": author or "未知",
                        "likes": _format_xhs_count(likes),
                        "comments": _format_xhs_count(comments_cnt),
                        "collects": _format_xhs_count(collects),
                        "tags": tags,
                        "content_type": note_type,
                        "images": images,
                        "image_count": len(images),
                        "extra_info": f"👤 {author or '未知'} · ❤️ {_format_xhs_count(likes)} · 💬 {_format_xhs_count(comments_cnt)} · ⭐ {_format_xhs_count(collects)}",
                    }
                    if images:
                        result["cover_image"] = images[0]
                    return result

        except Exception as e:
            logger.warning("方案B(xhs_cli API)失败: note_id=%s, error=%s", note_id, e)
    else:
        logger.info("方案B(xhs_cli API)跳过: XhsClient未初始化")

    # ── 方案 C: 降级到 HTML 解析（可能被反爬拿到空壳数据）──
    logger.info("方案C(HTML解析)开始: note_id=%s", note_id)
    note_url = f"https://www.xiaohongshu.com/explore/{note_id}"
    xhs_headers = {
        **BROWSER_HEADERS,
        "Referer": "https://www.xiaohongshu.com/",
        "Origin": "https://www.xiaohongshu.com",
    }

    try:
        resp = await client.get(note_url, headers=xhs_headers, follow_redirects=True)
        resp.raise_for_status()
        html = resp.text

        pattern = r'window\.__INITIAL_STATE__\s*=\s*({.+})\s*</script>'
        match = re.search(pattern, html, re.DOTALL)

        if match:
            try:
                raw = match.group(1).replace("undefined", "null")
                data = json.loads(raw)

                note_data = data.get("note", {}).get("noteDetailMap", {})
                if note_data:
                    first_key = next(iter(note_data))
                    detail = note_data[first_key].get("note", {})

                    title = detail.get("title", "")
                    desc = detail.get("desc", "")
                    author = detail.get("user", {}).get("nickname", "")

                    if bool(title) or bool(desc) or bool(author):
                        likes = detail.get("interactInfo", {}).get("likedCount", 0)
                        comments_cnt = detail.get("interactInfo", {}).get("commentCount", 0)
                        collects = detail.get("interactInfo", {}).get("collectedCount", 0)
                        tags = [tag.get("name", "") for tag in detail.get("tagList", [])]
                        note_type = "视频" if detail.get("type") == "video" else "图文"

                        summary_text = desc if desc else title
                        if len(summary_text) > 300:
                            summary_text = summary_text[:300] + "..."

                        return {
                            "title": title or "小红书笔记",
                            "summary": summary_text,
                            "author": author or "未知",
                            "likes": likes,
                            "comments": comments_cnt,
                            "collects": collects,
                            "tags": tags,
                            "content_type": note_type,
                            "extra_info": f"👤 {author or '未知'} · ❤️ {likes} · 💬 {comments_cnt} · ⭐ {collects}",
                            "_fallback": True,
                        }
            except (json.JSONDecodeError, StopIteration, KeyError):
                pass

        result = _parse_generic_html(html, note_url)
        # 检测错误页面（如 "你访问的页面不见了"）
        error_patterns = ["页面不见了", "页面不存在", "访问受限", "请在app内打开"]
        title_str = result.get("title", "")
        summary_str = result.get("summary", "")
        if any(p in title_str for p in error_patterns) or "沪ICP备" in summary_str:
            logger.warning("方案C(HTML解析): 检测到错误页面, title=%s", title_str)
            return {"error": f"小红书返回错误页面: {title_str}", "note_id": note_id}
        result["_fallback"] = True
        return result

    except Exception as e:
        logger.error("方案C(HTML解析)异常: note_id=%s, error=%s", note_id, e)
        return {"error": f"请求小红书失败: {str(e)}", "note_id": note_id}


# ═══════════════════════════════════════════════════════════════════
#  YouTube 解析
# ═══════════════════════════════════════════════════════════════════

def _extract_youtube_id(url: str) -> str | None:
    """提取 YouTube 视频 ID"""
    patterns = [
        r'youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
        r'youtu\.be/([a-zA-Z0-9_-]{11})',
        r'youtube\.com/embed/([a-zA-Z0-9_-]{11})',
        r'youtube\.com/shorts/([a-zA-Z0-9_-]{11})',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


async def parse_youtube(url: str, client: httpx.AsyncClient) -> dict:
    """解析 YouTube 视频信息"""
    video_id = _extract_youtube_id(url)
    if not video_id:
        return {"error": "无法提取YouTube视频ID"}

    # 使用 oembed API（无需 API key）
    oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
    try:
        resp = await client.get(oembed_url, follow_redirects=True)
        if resp.status_code == 200:
            data = resp.json()
            title = data.get("title", "")
            author = data.get("author_name", "")

            # 再请求页面获取描述
            page_resp = await client.get(
                f"https://www.youtube.com/watch?v={video_id}",
                headers=BROWSER_HEADERS,
                follow_redirects=True,
            )
            description = ""
            if page_resp.status_code == 200:
                # 从 meta 标签获取描述
                soup = BeautifulSoup(page_resp.text, "html.parser")
                desc_meta = soup.find("meta", {"name": "description"}) or soup.find("meta", {"property": "og:description"})
                if desc_meta:
                    description = desc_meta.get("content", "")

            summary = description if description else f"YouTube 视频：{title}"
            if len(summary) > 300:
                summary = summary[:300] + "..."

            return {
                "title": title,
                "summary": summary,
                "author": author,
                "content_type": "视频",
                "extra_info": f"👤 {author}",
            }
    except Exception:
        pass

    # 降级到页面抓取
    try:
        resp = await client.get(url, headers=BROWSER_HEADERS, follow_redirects=True)
        return _parse_generic_html(resp.text, url)
    except Exception as e:
        return {"error": f"请求YouTube失败: {str(e)}"}


# ═══════════════════════════════════════════════════════════════════
#  B站解析
# ═══════════════════════════════════════════════════════════════════

def _extract_bilibili_id(url: str) -> str | None:
    """提取B站视频 BV号"""
    patterns = [
        r'bilibili\.com/video/(BV[a-zA-Z0-9]+)',
        r'b23\.tv/([a-zA-Z0-9]+)',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


async def parse_bilibili(url: str, client: httpx.AsyncClient) -> dict:
    """解析B站视频信息"""
    bvid = _extract_bilibili_id(url)

    # 如果是短链接，先跟随重定向
    if "b23.tv" in url:
        try:
            resp = await client.get(url, follow_redirects=True)
            final_url = str(resp.url)
            bvid = _extract_bilibili_id(final_url)
        except Exception:
            pass

    if bvid and bvid.startswith("BV"):
        # 使用 B站 API 获取视频信息
        api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        try:
            resp = await client.get(api_url, headers=BROWSER_HEADERS)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    v = data["data"]
                    title = v.get("title", "")
                    desc = v.get("desc", "")
                    author = v.get("owner", {}).get("name", "")
                    view = v.get("stat", {}).get("view", 0)
                    like = v.get("stat", {}).get("like", 0)
                    coin = v.get("stat", {}).get("coin", 0)
                    danmaku = v.get("stat", {}).get("danmaku", 0)

                    summary = desc if desc and desc != "-" else title
                    if len(summary) > 300:
                        summary = summary[:300] + "..."

                    # 格式化播放数
                    def fmt_num(n):
                        if n >= 10000:
                            return f"{n/10000:.1f}万"
                        return str(n)

                    return {
                        "title": title,
                        "summary": summary,
                        "author": author,
                        "content_type": "视频",
                        "extra_info": f"👤 {author} · ▶️ {fmt_num(view)} · 👍 {fmt_num(like)} · 🪙 {fmt_num(coin)} · 💬 {fmt_num(danmaku)}弹幕",
                    }
        except Exception:
            pass

    # 降级到页面抓取
    try:
        target = url if "b23.tv" not in url else f"https://www.bilibili.com/video/{bvid}" if bvid else url
        resp = await client.get(target, headers=BROWSER_HEADERS, follow_redirects=True)
        return _parse_generic_html(resp.text, target)
    except Exception as e:
        return {"error": f"请求B站失败: {str(e)}"}


# ═══════════════════════════════════════════════════════════════════
#  知乎解析
# ═══════════════════════════════════════════════════════════════════

async def parse_zhihu(url: str, client: httpx.AsyncClient) -> dict:
    """解析知乎文章/问答"""
    try:
        zhihu_headers = {
            **BROWSER_HEADERS,
            "Referer": "https://www.zhihu.com/",
        }
        resp = await client.get(url, headers=zhihu_headers, follow_redirects=True)
        resp.raise_for_status()
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        title = ""
        description = ""
        author = ""

        # 提取 meta 标签
        og_title = soup.find("meta", {"property": "og:title"})
        og_desc = soup.find("meta", {"property": "og:description"})
        if og_title:
            title = og_title.get("content", "")
        if og_desc:
            description = og_desc.get("content", "")

        # 尝试找作者
        author_meta = soup.find("meta", {"itemprop": "author"}) or soup.find("meta", {"name": "author"})
        if author_meta:
            author = author_meta.get("content", "")

        # 尝试从页面提取正文
        article = soup.find("div", {"class": "RichContent-inner"}) or soup.find("div", {"class": "Post-RichTextContainer"})
        body_text = ""
        if article:
            body_text = article.get_text(strip=True)
            if len(body_text) > 500:
                body_text = body_text[:500] + "..."

        summary = body_text if body_text and len(body_text) > len(description) else description
        if not summary:
            summary = title
        if len(summary) > 300:
            summary = summary[:300] + "..."

        result = {
            "title": title or "知乎内容",
            "summary": summary,
            "content_type": "文章" if "/p/" in url else "问答",
        }
        if author:
            result["author"] = author
            result["extra_info"] = f"👤 {author}"
        return result

    except Exception as e:
        return {"error": f"请求知乎失败: {str(e)}"}


# ═══════════════════════════════════════════════════════════════════
#  通用 HTML 解析（兜底方案）
# ═══════════════════════════════════════════════════════════════════

def _parse_generic_html(html: str, url: str) -> dict:
    """从 HTML 提取标题、描述、正文摘要"""
    soup = BeautifulSoup(html, "html.parser")

    # 标题
    title = ""
    for selector in [
        ("meta", {"property": "og:title"}),
        ("meta", {"name": "twitter:title"}),
        ("title", {}),
    ]:
        tag = soup.find(selector[0], selector[1])
        if tag:
            title = tag.get("content", "") if selector[0] == "meta" else tag.get_text(strip=True)
            if title:
                break

    # 描述
    description = ""
    for selector in [
        ("meta", {"property": "og:description"}),
        ("meta", {"name": "twitter:description"}),
        ("meta", {"name": "description"}),
    ]:
        tag = soup.find(selector[0], selector[1])
        if tag:
            description = tag.get("content", "").strip()
            if description:
                break

    # 正文提取
    # 移除无用标签
    for tag_name in ["script", "style", "nav", "footer", "header", "aside", "iframe", "noscript", "svg"]:
        for el in soup.find_all(tag_name):
            el.decompose()

    body_text = ""
    # 尝试文章正文区域
    article = (
        soup.find("article")
        or soup.find(attrs={"role": "main"})
        or soup.find("div", {"class": re.compile(r"article|content|post|entry|main", re.I)})
    )
    if article:
        body_text = article.get_text(separator=" ", strip=True)
    else:
        body = soup.find("body")
        if body:
            body_text = body.get_text(separator=" ", strip=True)

    # 清理
    body_text = re.sub(r'\s+', ' ', body_text).strip()
    if len(body_text) > 1500:
        body_text = body_text[:1500]

    # 选择最佳摘要
    if description and len(description) > 30:
        summary = description
    elif body_text and len(body_text) > 50:
        # 取前几个有意义的句子
        sentences = re.split(r'[。！？.!?\n]+', body_text)
        meaningful = [s.strip() for s in sentences if len(s.strip()) > 10][:5]
        summary = "。".join(meaningful)
        if len(summary) > 300:
            summary = summary[:300] + "..."
    elif description:
        summary = description
    else:
        summary = f"{title}" if title else ""

    if not title and not summary:
        return {"error": "页面内容为空或无法解析"}

    # 作者
    author = ""
    author_meta = soup.find("meta", {"name": "author"}) or soup.find("meta", {"property": "article:author"})
    if author_meta:
        author = author_meta.get("content", "")

    result = {
        "title": title or "未获取到标题",
        "summary": summary if summary else "未获取到摘要",
    }
    if author:
        result["author"] = author
        result["extra_info"] = f"👤 {author}"

    return result


# ═══════════════════════════════════════════════════════════════════
#  路由调度
# ═══════════════════════════════════════════════════════════════════

PLATFORM_PARSERS = {
    "xiaohongshu": parse_xiaohongshu,
    "youtube": parse_youtube,
    "bilibili": parse_bilibili,
    "zhihu": parse_zhihu,
}


async def _parse_url(url: str) -> dict:
    """统一解析入口"""
    platform = identify_platform(url)

    async with httpx.AsyncClient(
        headers=BROWSER_HEADERS,
        follow_redirects=True,
        timeout=20.0,
    ) as client:
        parser = PLATFORM_PARSERS.get(platform["id"])
        if parser:
            result = await parser(url, client)
        else:
            # 通用解析
            try:
                resp = await client.get(url, follow_redirects=True)
                resp.raise_for_status()
                result = _parse_generic_html(resp.text, url)
            except Exception as e:
                result = {"error": f"请求失败: {str(e)}"}

    # 附加平台信息
    result["platform"] = platform
    result["url"] = url
    return result


# ═══════════════════════════════════════════════════════════════════
#  API 端点
# ═══════════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    return {"service": "Link Parser API", "version": "1.0.0", "status": "ok"}


def _clean_url(url: str) -> str:
    """清洗 URL：去除末尾中文标点、全角符号等非法字符（不删除中文汉字）"""
    # 只去除末尾的中文/全角标点符号（不包括 CJK 统一汉字 \u4e00-\u9fff）
    url = re.sub(r'[\uff00-\uffef\u3000-\u303f，。！？、；：""''【】（）]+$', '', url)
    # 去除末尾的英文标点残留
    url = re.sub(r'[,;:!?)}\]]+$', '', url)
    return url.strip()


@app.get("/parse")
async def parse_link(url: str = Query(..., description="要解析的链接")):
    """
    解析链接内容，返回标题、摘要、作者等信息。
    支持小红书、YouTube、B站、知乎等主流平台。
    """
    if not url:
        return JSONResponse(status_code=400, content={"error": "缺少 url 参数"})

    # 清洗 URL（去除中文标点等脏字符）
    url = _clean_url(url)
    logger.info("解析链接: %s", url)

    # 自动补全 https
    if not url.startswith("http"):
        url = "https://" + url

    try:
        result = await _parse_url(url)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error("解析失败: %s — %s", url, e)
        return JSONResponse(
            status_code=500,
            content={"error": f"解析失败: {str(e)}", "url": url},
        )


@app.get("/health")
async def health():
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════
#  灵感碰撞引擎 — IMA 知识库 + AI 深度思考
# ═══════════════════════════════════════════════════════════════════

# IMA API 配置（从环境变量读取）
IMA_CLIENT_ID = os.environ.get("IMA_CLIENT_ID", "")
IMA_API_KEY = os.environ.get("IMA_API_KEY", "")
IMA_API_BASE = "https://ima.qq.com"

# 智谱 GLM AI API（用于生成碰撞观点，GLM-4-Flash 免费）
GLM_API_KEY = os.environ.get("GLM_API_KEY", "")
GLM_API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


async def _ima_api(path: str, body: dict) -> dict | None:
    """调用 IMA OpenAPI"""
    if not IMA_CLIENT_ID or not IMA_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{IMA_API_BASE}/{path}",
                headers={
                    "ima-openapi-clientid": IMA_CLIENT_ID,
                    "ima-openapi-apikey": IMA_API_KEY,
                    "Content-Type": "application/json",
                },
                json=body,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("retcode", 0) == 0:
                    return data.get("data", data)
                return data
    except Exception as e:
        logger.warning("IMA API 调用失败: %s — %s", path, e)
    return None


def _extract_keywords(text: str, max_keywords: int = 5) -> list[str]:
    """从文本中提取搜索关键词（基于规则的轻量方案，无需额外依赖）"""
    # 去除常见停用词和标点
    stop_words = {
        "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "一个",
        "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好",
        "自己", "这", "他", "她", "它", "们", "那", "这个", "那个", "什么", "怎么",
        "可以", "能", "但是", "而且", "因为", "所以", "如果", "或者", "还是", "已经",
        "其实", "可能", "应该", "觉得", "感觉", "这样", "那样", "比较", "非常",
        "真的", "确实", "但", "而", "又", "也", "还", "才", "吧", "吗", "呢", "啊",
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "shall",
        "should", "may", "might", "must", "can", "could", "about", "above",
        "after", "before", "between", "but", "by", "for", "from", "in", "into",
        "of", "on", "or", "out", "over", "so", "than", "that", "this", "to",
        "under", "up", "with", "and", "not", "no", "it", "its", "i", "my", "me",
        "we", "you", "your", "he", "she", "they", "them", "their", "what", "how",
    }

    # 提取英文词和中文短语
    # 英文：提取完整单词（2字母以上）
    en_words = re.findall(r'[a-zA-Z]{2,}', text)
    en_keywords = [w.lower() for w in en_words if w.lower() not in stop_words and len(w) >= 3]

    # 中文：简单的 n-gram 切分（2-4字），偏向名词/概念性短语
    cn_text = re.sub(r'[a-zA-Z0-9\s\.,;:!?\-\'"()\[\]{}<>@#$%^&*+=|/\\~`，。！？、；：""''【】（）《》]', ' ', text)
    cn_segments = cn_text.split()

    cn_keywords = []
    for seg in cn_segments:
        seg = seg.strip()
        if len(seg) < 2:
            continue
        if seg in stop_words:
            continue
        # 2-6字的中文短语作为关键词
        if 2 <= len(seg) <= 6:
            cn_keywords.append(seg)
        elif len(seg) > 6:
            # 长段切成 2-4 字的滑动窗口，取头尾
            cn_keywords.append(seg[:4])
            if len(seg) > 4:
                cn_keywords.append(seg[-4:])

    # 合并去重，优先英文专有名词和中文短语
    seen = set()
    keywords = []
    for kw in en_keywords + cn_keywords:
        if kw not in seen:
            seen.add(kw)
            keywords.append(kw)

    return keywords[:max_keywords]


def _compute_relevance_score(query_text: str, title: str, highlight: str) -> float:
    """计算搜索结果与原始内容的相关性分数（0-1）"""
    query_lower = query_text.lower()
    title_lower = (title or "").lower()
    highlight_lower = (highlight or "").lower()

    # 提取 query 的关键词
    query_keywords = _extract_keywords(query_text, max_keywords=8)
    if not query_keywords:
        return 0.3  # 无法提取关键词时给中等分

    matched = 0
    total = len(query_keywords)

    for kw in query_keywords:
        kw_l = kw.lower()
        # 在标题中匹配权重更高
        if kw_l in title_lower:
            matched += 1.5
        elif kw_l in highlight_lower:
            matched += 1.0

    score = min(matched / max(total, 1), 1.0)
    return score


async def _search_ima_knowledge_bases(query: str, limit: int = 5) -> list[dict]:
    """搜索所有知识库中的相关内容（优化版：关键词提取 + 相关性过滤）"""
    # 1. 获取用户的知识库列表
    kb_list = await _ima_api("openapi/wiki/v1/search_knowledge_base", {
        "query": "", "cursor": "", "limit": 20
    })
    if not kb_list or not kb_list.get("info_list"):
        return []

    # 2. 从内容中提取关键词，用关键词搜索（而不是原文前100字）
    keywords = _extract_keywords(query, max_keywords=5)
    search_query = " ".join(keywords) if keywords else query[:50]
    logger.info("知识库搜索关键词: %s (原文: %s...)", search_query, query[:60])

    # 3. 搜索更多知识库（前10个），但会在后续过滤不相关结果
    all_kbs = kb_list["info_list"][:10]

    async def search_one_kb(kb_id: str, kb_name: str) -> list[dict]:
        """搜索单个知识库，返回结果列表（不再 append 共享变量）"""
        kb_results = []
        data = await _ima_api("openapi/wiki/v1/search_knowledge", {
            "query": search_query, "knowledge_base_id": kb_id, "cursor": ""
        })
        if data and data.get("info_list"):
            for item in data["info_list"][:3]:
                title = item.get("title", "")
                highlight = item.get("highlight_content", "")
                # 计算相关性分数
                score = _compute_relevance_score(query, title, highlight)
                kb_results.append({
                    "title": title,
                    "highlight": highlight,
                    "kb_name": kb_name,
                    "media_id": item.get("media_id", ""),
                    "_relevance_score": score,
                })
        return kb_results

    tasks = [search_one_kb(kb["id"], kb.get("name", "")) for kb in all_kbs]
    gather_results = await asyncio.gather(*tasks, return_exceptions=True)

    # 合并所有结果，跳过异常
    results = []
    for i, res in enumerate(gather_results):
        if isinstance(res, Exception):
            kb_name = all_kbs[i].get("name", "unknown") if i < len(all_kbs) else "unknown"
            logger.warning("知识库 '%s' 搜索异常: %s", kb_name, res)
        elif isinstance(res, list):
            results.extend(res)

    # 4. 按相关性排序，过滤掉明显不相关的结果（分数 < 0.15）
    relevant = [r for r in results if r.get("_relevance_score", 0) >= 0.15]
    relevant.sort(key=lambda x: x.get("_relevance_score", 0), reverse=True)

    logger.info("知识库搜索结果: 总 %d 条, 过滤后 %d 条 (阈值>=0.15)",
                len(results), len(relevant))
    if results and not relevant:
        # 如果全部被过滤，保留分数最高的1条（至少给 AI 一些参考）
        results.sort(key=lambda x: x.get("_relevance_score", 0), reverse=True)
        relevant = results[:1]

    return relevant[:8]  # 最多返回8条


async def _search_ima_notes(query: str) -> list[dict]:
    """搜索 IMA 笔记中的相关内容"""
    results = []
    data = await _ima_api("openapi/note/v1/search_note_book", {
        "search_type": 1,  # 正文搜索
        "query_info": {"content": query},
        "start": 0, "end": 5,
    })
    if data and data.get("docs"):
        for doc in data["docs"][:5]:
            basic = doc.get("doc", {}).get("basic_info", {})
            results.append({
                "title": basic.get("title", ""),
                "summary": basic.get("summary", ""),
                "docid": basic.get("docid", ""),
                "folder_name": basic.get("folder_name", ""),
            })
    return results


async def _generate_insight_with_ai(
    new_content: str,
    content_type: str,
    knowledge_items: list[dict],
    note_items: list[dict],
    history_items: list[dict],
) -> str:
    """用 AI 生成真正有深度的灵感碰撞"""
    if not GLM_API_KEY:
        # 没有 AI API Key 时，用纯规则生成（但比简单匹配好）
        return _generate_rule_based_insight(
            new_content, knowledge_items, note_items, history_items
        )

    # 构建 context
    context_parts = []

    if knowledge_items:
        kb_text = "\n".join([
            f"- 【{k.get('kb_name', '知识库')}】{k.get('title', '')}：{k.get('highlight', '')[:200]}"
            f"（相关度：{'高' if k.get('_relevance_score', 0) >= 0.5 else '中' if k.get('_relevance_score', 0) >= 0.25 else '低'}）"
            for k in knowledge_items[:5]
        ])
        context_parts.append(f"## 知识库中的搜索结果（注意：低相关度的条目可能与用户想法无关，请自行判断）\n{kb_text}")

    if note_items:
        note_text = "\n".join([
            f"- 【{n.get('folder_name', '笔记')}】{n.get('title', '')}：{n.get('summary', '')[:150]}"
            for n in note_items[:3]
        ])
        context_parts.append(f"## 笔记中的相关内容\n{note_text}")

    if history_items:
        hist_text = "\n".join([
            f"- [{h.get('type', '')}] {h.get('content', '')[:150]}"
            for h in history_items[:5]
        ])
        context_parts.append(f"## 历史记录中的相关想法\n{hist_text}")

    context = "\n\n".join(context_parts) if context_parts else "（没有找到直接相关的参考资料）"

    prompt = f"""你是一个思维碰撞助手。用户刚刚记录了一条新想法，你需要基于参考资料进行深度思维碰撞，产生真正有价值的启发。

## 用户刚记录的内容
类型：{content_type}
内容：{new_content}

## 参考资料
{context}

## 核心原则

### 关于参考资料的使用
- **只用真正相关的参考资料**。如果某条参考资料和用户的想法没有实质关联（主题不同、领域无关），直接忽略它，不要勉强建立联系
- 宁可基于用户的想法本身做深度延伸，也不要硬塞不相关的知识库内容
- 如果参考资料中确实有高度相关的内容，自然地融入回应，说明启发来源

### 关于碰撞质量
1. **产生新观点** — 把不同来源的信息交叉组合，发现矛盾、互补、延伸的可能性
2. **提出有力的反问** — "你有没有想过...?"、"如果把X和Y结合会怎样?"
3. **指出有趣的张力** — "你的这个想法和XX存在一个有趣的矛盾..."
4. **深度延伸** — 基于用户的想法往更深处推进一步，提供他可能没想到的视角
5. **绝对不要**说"你之前写过类似的"这种无用信息

### 关于语气
- 像一个思维敏锐的朋友在跟你聊天
- 不要用"以下是我的分析"、"总结来说"之类的套话
- 可以犀利、可以温和，但一定要有观点
- 如果用户的想法有盲区，大胆指出来

直接输出灵感内容（100-200字），不要加标题、不要用markdown格式。"""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                GLM_API_URL,
                headers={
                    "Authorization": f"Bearer {GLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "glm-4-flash",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.8,
                    "max_tokens": 400,
                    "stream": False,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "").strip()
    except Exception as e:
        logger.warning("AI 生成失败: %s", e)

    # AI 调用失败，降级到规则生成
    return _generate_rule_based_insight(
        new_content, knowledge_items, note_items, history_items
    )


def _generate_rule_based_insight(
    new_content: str,
    knowledge_items: list[dict],
    note_items: list[dict],
    history_items: list[dict],
) -> str:
    """无 AI 时的规则灵感生成 — 比简单关键词匹配更有深度"""
    parts = []

    if knowledge_items:
        kb = knowledge_items[0]
        kb_title = kb.get("title", "")
        kb_name = kb.get("kb_name", "知识库")
        highlight = kb.get("highlight", "")[:100]
        if highlight:
            parts.append(
                f"你的「{kb_name}」里有一篇《{kb_title}》提到了类似的话题"
                f"——\"{highlight}\"。把你刚才写的和这个放在一起看，有没有发现新的角度？"
            )
        elif kb_title:
            parts.append(
                f"你在「{kb_name}」里收藏过《{kb_title}》，和你刚记录的这条可能存在有趣的交集。"
                f"不妨回头看看那篇内容，也许能碰撞出新的想法。"
            )

    if note_items and not parts:
        note = note_items[0]
        note_title = note.get("title", "")
        folder = note.get("folder_name", "笔记")
        summary = note.get("summary", "")[:80]
        if summary:
            parts.append(
                f"你在「{folder}」笔记本里有一篇《{note_title}》写道：\"{summary}\"。"
                f"对比你刚才的想法，这两条思考之间可能存在值得深挖的联系。"
            )

    if history_items:
        h = history_items[0]
        h_content = h.get("content", "")[:80]
        h_type = h.get("type", "")
        type_map = {"thought": "想法", "inspiration": "灵感", "reflection": "感悟",
                    "product": "产品思考", "quote": "摘录", "todo": "待办"}
        h_label = type_map.get(h_type, "记录")
        if h_content:
            if parts:
                parts.append(
                    f"加上你之前的一条{h_label}「{h_content}」——三者之间是否在描述同一个底层模式？"
                )
            else:
                parts.append(
                    f"你之前有条{h_label}写过「{h_content}」，和这次的想法在主题上有呼应。"
                    f"如果把这两个时间点的思考连起来看，你的认知可能已经在某个方向上进化了。"
                )

    if not parts:
        # 完全没有相关内容 — 给一个有思考性的鼓励
        if len(new_content) > 80:
            return "这条记录内容丰富，是一次深度思考。它暂时还是个独立的思考节点——随着你继续记录，它会自然地与未来的想法产生连接。"
        else:
            return "新的思考种子已经种下。现在它看起来独立，但好想法往往在积累到一定量后突然产生化学反应。"

    return " ".join(parts)


@app.post("/inspire")
async def inspire(body: dict = Body(...)):
    """
    灵感碰撞引擎：接收新记录内容，搜索 IMA 知识库和笔记，
    结合历史记录生成有深度的灵感启迪。

    Request body:
    {
        "content": "用户刚记录的内容",
        "type": "thought",
        "history": [{"content": "...", "type": "..."}, ...]  // 最近几条历史记录
    }
    """
    content = str(body.get("content", "")).strip()
    content_type = str(body.get("type", "thought")).strip()
    history = body.get("history", [])

    if not content:
        return JSONResponse(status_code=400, content={"error": "content 不能为空"})

    # 输入验证：content 长度限制（防止超长内容打爆 AI 上下文）
    MAX_CONTENT_LENGTH = 5000
    if len(content) > MAX_CONTENT_LENGTH:
        content = content[:MAX_CONTENT_LENGTH]
        logger.info("content 超长截断: %d -> %d", len(body.get("content", "")), MAX_CONTENT_LENGTH)

    # 输入验证：content_type 白名单校验（防止恶意值拼入 AI prompt）
    ALLOWED_TYPES = {"thought", "inspiration", "todo", "quote", "link", "reflection", "product"}
    if content_type not in ALLOWED_TYPES:
        content_type = "thought"

    # 输入验证：history 格式校验和限制
    if not isinstance(history, list):
        history = []
    validated_history = []
    for h in history[:8]:  # 最多8条历史
        if isinstance(h, dict):
            h_content = str(h.get("content", ""))[:200]  # 每条最多200字
            h_type = h.get("type", "thought")
            if h_type not in ALLOWED_TYPES:
                h_type = "thought"
            validated_history.append({"content": h_content, "type": h_type})
    history = validated_history

    type_labels = {
        "thought": "想法", "inspiration": "灵感", "todo": "待办",
        "quote": "摘录", "link": "链接", "reflection": "感悟", "product": "产品思考"
    }
    type_label = type_labels.get(content_type, content_type)

    # 提取搜索关键词（从内容中提取核心概念，而不是直接用原文）
    keywords = _extract_keywords(content, max_keywords=5)
    search_query = " ".join(keywords) if keywords else content[:50]
    logger.info("灵感碰撞搜索关键词: %s", search_query)

    # 并行搜索 IMA 知识库 + IMA 笔记
    kb_results, note_results = [], []

    if IMA_CLIENT_ID and IMA_API_KEY:
        kb_task = _search_ima_knowledge_bases(search_query)
        note_task = _search_ima_notes(search_query)
        kb_results, note_results = await asyncio.gather(
            kb_task, note_task, return_exceptions=True
        )
        if isinstance(kb_results, Exception):
            logger.warning("知识库搜索异常: %s", kb_results)
            kb_results = []
        if isinstance(note_results, Exception):
            logger.warning("笔记搜索异常: %s", note_results)
            note_results = []

    # 生成灵感碰撞
    insight_text = await _generate_insight_with_ai(
        new_content=content,
        content_type=type_label,
        knowledge_items=kb_results,
        note_items=note_results,
        history_items=history[:5],
    )

    # 构建关联来源
    sources = []
    for k in (kb_results or [])[:3]:
        sources.append({
            "type": "knowledge",
            "title": k.get("title", ""),
            "from": k.get("kb_name", "知识库"),
        })
    for n in (note_results or [])[:2]:
        sources.append({
            "type": "note",
            "title": n.get("title", ""),
            "from": n.get("folder_name", "笔记"),
        })

    return JSONResponse(content={
        "insight": insight_text,
        "sources": sources,
        "has_knowledge": len(kb_results or []) > 0,
        "has_notes": len(note_results or []) > 0,
        "has_ai": bool(GLM_API_KEY),
    })


# ═══════════════════════════════════════════════════════════════════
#  启动
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8900))
    uvicorn.run(app, host="0.0.0.0", port=port)
