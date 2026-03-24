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
from urllib.parse import quote, urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
#  小红书解析（优先使用 xhs_cli API，降级到 HTML 解析）
# ═══════════════════════════════════════════════════════════════════

def _extract_xhs_note_id(url: str) -> str | None:
    """从小红书链接提取笔记 ID"""
    # https://www.xiaohongshu.com/explore/xxx
    # https://www.xiaohongshu.com/discovery/item/xxx
    patterns = [
        r'xiaohongshu\.com/(?:explore|discovery/item)/([a-zA-Z0-9]+)',
        r'xhslink\.com/([a-zA-Z0-9]+)',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def _format_xhs_count(count) -> str:
    """格式化小红书互动数"""
    if isinstance(count, str):
        return count
    if isinstance(count, (int, float)):
        if count >= 10000:
            return f"{count/10000:.1f}万"
        return str(int(count))
    return str(count) if count else "0"


async def _resolve_xhs_short_link(url: str, client: httpx.AsyncClient) -> str | None:
    """解析小红书短链接，返回最终笔记 ID"""
    try:
        resp = await client.get(url, follow_redirects=True)
        final_url = str(resp.url)
        return _extract_xhs_note_id(final_url)
    except Exception:
        return None


async def parse_xiaohongshu(url: str, client: httpx.AsyncClient) -> dict:
    """解析小红书笔记内容 — 优先 xhs_cli API，降级 HTML"""
    note_id = _extract_xhs_note_id(url)

    # 如果是短链接，先跟随重定向拿到笔记 ID
    if "xhslink.com" in url or not note_id:
        note_id = await _resolve_xhs_short_link(url, client)

    if not note_id:
        return {"error": "无法从链接提取笔记ID"}

    # ── 方案 A: 使用 xhs_cli API（需要有效 Cookie）──
    xhs = _get_xhs_client()
    if xhs:
        try:
            detail = xhs.get_note_detail(note_id)
            if detail:
                # get_note_detail 可能返回两种格式
                # 1) 来自 get_note_from_html: {title, desc, user, ...}
                # 2) 来自 get_note_by_id (feed API): {noteId, type, desc, title, user, imageList, ...}
                title = detail.get("title", "") or detail.get("displayTitle", "")
                desc = detail.get("desc", "")
                user = detail.get("user", {})
                author = user.get("nickname", "") or user.get("nick_name", "")

                # 互动数据 — 兼容两种 key 命名
                interact = detail.get("interactInfo", detail.get("interact_info", {}))
                likes = interact.get("likedCount", interact.get("liked_count", "0"))
                comments = interact.get("commentCount", interact.get("comment_count", "0"))
                collects = interact.get("collectedCount", interact.get("collected_count", "0"))
                shares = interact.get("shareCount", interact.get("share_count", "0"))

                # 图片
                image_list = detail.get("imageList", detail.get("image_list", []))

                # 标签
                tags = []
                for tag in detail.get("tagList", detail.get("tag_list", [])):
                    tag_name = tag.get("name", "") if isinstance(tag, dict) else str(tag)
                    if tag_name:
                        tags.append(tag_name)

                # 类型
                note_type_raw = detail.get("type", "normal")
                note_type = "视频" if note_type_raw == "video" else "图文"

                # 有实际内容？
                if title or desc or author:
                    summary_text = desc if desc else title
                    if len(summary_text) > 300:
                        summary_text = summary_text[:300] + "..."

                    result = {
                        "title": title or "小红书笔记",
                        "summary": summary_text,
                        "author": author or "未知",
                        "likes": _format_xhs_count(likes),
                        "comments": _format_xhs_count(comments),
                        "collects": _format_xhs_count(collects),
                        "tags": tags,
                        "content_type": note_type,
                        "extra_info": f"👤 {author or '未知'} · ❤️ {_format_xhs_count(likes)} · 💬 {_format_xhs_count(comments)} · ⭐ {_format_xhs_count(collects)}",
                    }
                    # 附加首张图片 URL
                    if image_list:
                        first_img = image_list[0]
                        img_url = ""
                        # 格式 1: {urlPre: "..."} 或 {url_pre: "..."}
                        img_url = first_img.get("urlPre", first_img.get("url_pre", ""))
                        # 格式 2: {info_list: [{url: "..."}]} 或 {infoList: [{url: "..."}]}
                        if not img_url:
                            info_list = first_img.get("infoList", first_img.get("info_list", []))
                            if info_list:
                                img_url = info_list[0].get("url", "")
                        if not img_url:
                            img_url = first_img.get("url", "")
                        if img_url:
                            result["cover_image"] = img_url
                    return result

        except Exception as e:
            logger.warning("xhs_cli API 解析失败 (note_id=%s): %s", note_id, e)
            # 降级到方案 B

    # ── 方案 B: 降级到 HTML 解析（可能被反爬拿到空壳数据）──
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

        # 从 __INITIAL_STATE__ 提取结构化数据
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

                    has_real_content = bool(title) or bool(desc) or bool(author)
                    if has_real_content:
                        likes = detail.get("interactInfo", {}).get("likedCount", 0)
                        comments = detail.get("interactInfo", {}).get("commentCount", 0)
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
                            "comments": comments,
                            "collects": collects,
                            "tags": tags,
                            "content_type": note_type,
                            "extra_info": f"👤 {author or '未知'} · ❤️ {likes} · 💬 {comments} · ⭐ {collects}",
                            "_fallback": True,
                        }
            except (json.JSONDecodeError, StopIteration, KeyError):
                pass

        # 最终降级：从 meta 标签解析
        result = _parse_generic_html(html, note_url)
        result["_fallback"] = True
        return result

    except Exception as e:
        return {"error": f"请求小红书失败: {str(e)}"}


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


@app.get("/parse")
async def parse_link(url: str = Query(..., description="要解析的链接")):
    """
    解析链接内容，返回标题、摘要、作者等信息。
    支持小红书、YouTube、B站、知乎等主流平台。
    """
    if not url:
        return JSONResponse(status_code=400, content={"error": "缺少 url 参数"})

    # 自动补全 https
    if not url.startswith("http"):
        url = "https://" + url

    try:
        result = await _parse_url(url)
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"解析失败: {str(e)}", "url": url},
        )


@app.get("/debug/xhs")
async def debug_xhs():
    """诊断 xhs_cli 状态（调试用）"""
    info = {
        "xhs_cookies_set": bool(os.environ.get("XHS_COOKIES", "")),
        "xhs_cookies_len": len(os.environ.get("XHS_COOKIES", "")),
    }
    try:
        from xhs_cli.client import XhsClient
        info["xhs_cli_import"] = True
    except Exception as e:
        info["xhs_cli_import"] = False
        info["xhs_cli_import_error"] = str(e)

    xhs = _get_xhs_client()
    info["xhs_client_ok"] = xhs is not None

    if xhs:
        try:
            detail = xhs.get_note_detail("695ba7ae000000000a028c2c")
            if detail:
                info["test_note_keys"] = list(detail.keys())[:15]
                info["test_note_title"] = (detail.get("title") or detail.get("displayTitle") or "")[:50]
                info["test_note_type"] = detail.get("type", "?")
                info["test_note_has_user"] = bool(detail.get("user"))
                info["test_note_has_desc"] = bool(detail.get("desc"))
                info["test_api_ok"] = bool(detail.get("title") or detail.get("desc") or detail.get("user"))
            else:
                info["test_api_ok"] = False
                info["test_detail_raw"] = "None"
        except Exception as e:
            info["test_api_ok"] = False
            info["test_api_error"] = str(e)[:200]

    # 直接 HTTP 测试
    import httpx as _httpx
    try:
        r = _httpx.get("https://www.xiaohongshu.com/explore/695ba7ae000000000a028c2c",
                       headers=BROWSER_HEADERS, follow_redirects=True, timeout=15)
        info["direct_http_status"] = r.status_code
        info["direct_http_has_initial_state"] = "__INITIAL_STATE__" in r.text
        # 检查 INITIAL_STATE 是否有真实内容
        import re as _re
        m = _re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*</script>', r.text, _re.DOTALL)
        if m:
            raw = m.group(1).replace("undefined", "null")
            d = json.loads(raw)
            nd = d.get("note", {}).get("noteDetailMap", {})
            if nd:
                fk = next(iter(nd))
                nt = nd[fk].get("note", {})
                info["html_title"] = nt.get("title", "")[:50]
                info["html_author"] = nt.get("user", {}).get("nickname", "")[:30]
                info["html_has_content"] = bool(nt.get("title") or nt.get("desc"))
            else:
                info["html_has_content"] = False
                info["html_note_map_empty"] = True
        else:
            info["html_initial_state_missing"] = True
    except Exception as e:
        info["direct_http_error"] = str(e)[:200]

    return JSONResponse(content=info)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════
#  启动
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8900))
    uvicorn.run(app, host="0.0.0.0", port=port)
