/**
 * Link Parser API — Deno Deploy 版本
 * 服务端抓取链接内容，解析标题/摘要/作者等信息
 * 支持：小红书、YouTube、B站、知乎、微信公众号、通用网页
 */

const BROWSER_HEADERS: Record<string, string> = {
  "User-Agent":
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
    "AppleWebKit/537.36 (KHTML, like Gecko) " +
    "Chrome/120.0.0.0 Safari/537.36",
  Accept:
    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
  "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
};

// ── Platform Identification ──
function identifyPlatform(url: string) {
  const u = url.toLowerCase();
  if (/xiaohongshu\.com|xhslink\.com|xhs\.cn/.test(u))
    return { id: "xiaohongshu", name: "小红书", icon: "📕" };
  if (/youtube\.com|youtu\.be/.test(u))
    return { id: "youtube", name: "YouTube", icon: "▶️" };
  if (/bilibili\.com|b23\.tv/.test(u))
    return { id: "bilibili", name: "B站", icon: "📺" };
  if (/zhihu\.com/.test(u))
    return { id: "zhihu", name: "知乎", icon: "💬" };
  if (/weixin\.qq\.com|mp\.weixin/.test(u))
    return { id: "wechat", name: "微信公众号", icon: "💚" };
  if (/douyin\.com/.test(u))
    return { id: "douyin", name: "抖音", icon: "🎵" };
  if (/twitter\.com|x\.com/.test(u))
    return { id: "twitter", name: "X/Twitter", icon: "🐦" };
  if (/github\.com/.test(u))
    return { id: "github", name: "GitHub", icon: "🐙" };
  if (/juejin\.cn/.test(u))
    return { id: "juejin", name: "掘金", icon: "💎" };
  if (/36kr\.com/.test(u))
    return { id: "36kr", name: "36氪", icon: "📰" };
  if (/sspai\.com/.test(u))
    return { id: "sspai", name: "少数派", icon: "📝" };
  if (/medium\.com/.test(u))
    return { id: "medium", name: "Medium", icon: "📖" };
  try {
    const host = new URL(url).hostname.replace("www.", "");
    return { id: "web", name: host, icon: "🔗" };
  } catch {
    return { id: "web", name: "网页", icon: "🔗" };
  }
}

// ── Fetch with timeout ──
async function fetchWithTimeout(
  url: string,
  opts: RequestInit = {},
  timeoutMs = 15000
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { ...opts, signal: controller.signal, redirect: "follow" });
    return res;
  } finally {
    clearTimeout(timer);
  }
}

// ── HTML Parsing helpers (no heavy libs, use regex on Deno) ──
function extractMeta(html: string, property: string): string {
  // Try property="..." then name="..."
  const patterns = [
    new RegExp(
      `<meta[^>]*property=["']${property}["'][^>]*content=["']([^"']+)["']`,
      "i"
    ),
    new RegExp(
      `<meta[^>]*content=["']([^"']+)["'][^>]*property=["']${property}["']`,
      "i"
    ),
    new RegExp(
      `<meta[^>]*name=["']${property}["'][^>]*content=["']([^"']+)["']`,
      "i"
    ),
    new RegExp(
      `<meta[^>]*content=["']([^"']+)["'][^>]*name=["']${property}["']`,
      "i"
    ),
  ];
  for (const p of patterns) {
    const m = html.match(p);
    if (m) return decodeHTMLEntities(m[1].trim());
  }
  return "";
}

function extractTitle(html: string): string {
  const m = html.match(/<title[^>]*>([^<]+)<\/title>/i);
  return m ? decodeHTMLEntities(m[1].trim()) : "";
}

function decodeHTMLEntities(text: string): string {
  return text
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&#x27;/g, "'")
    .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(parseInt(n)));
}

function stripTags(html: string): string {
  // Remove script/style blocks first
  let text = html.replace(/<(script|style|nav|footer|header|aside|noscript)[^>]*>[\s\S]*?<\/\1>/gi, " ");
  // Remove all tags
  text = text.replace(/<[^>]+>/g, " ");
  // Collapse whitespace
  text = text.replace(/\s+/g, " ").trim();
  return text;
}

function extractBodyContent(html: string): string {
  // Try to find article/main content
  const articleMatch = html.match(/<article[^>]*>([\s\S]*?)<\/article>/i)
    || html.match(/<div[^>]*class=["'][^"']*(?:article|content|post|entry|main)[^"']*["'][^>]*>([\s\S]*?)<\/div>/i);
  const raw = articleMatch ? articleMatch[1] : (html.match(/<body[^>]*>([\s\S]*?)<\/body>/i)?.[1] || html);
  let text = stripTags(raw);
  if (text.length > 2000) text = text.substring(0, 2000);
  return text;
}

// ── Generic HTML parser ──
function parseGenericHtml(html: string, url: string) {
  const title =
    extractMeta(html, "og:title") ||
    extractMeta(html, "twitter:title") ||
    extractTitle(html);
  const description =
    extractMeta(html, "og:description") ||
    extractMeta(html, "twitter:description") ||
    extractMeta(html, "description");
  const author = extractMeta(html, "author") || extractMeta(html, "article:author");
  const bodyText = extractBodyContent(html);

  // Pick best summary
  let summary = "";
  if (description && description.length > 30) {
    summary = description.length > 300 ? description.substring(0, 300) + "..." : description;
  } else if (bodyText.length > 50) {
    const sentences = bodyText.split(/[。！？.!?\n]+/).filter((s) => s.trim().length > 10);
    summary = sentences.slice(0, 4).join("。");
    if (summary.length > 300) summary = summary.substring(0, 300) + "...";
    if (summary.length < 20) summary = bodyText.substring(0, 200) + "...";
  } else if (description) {
    summary = description;
  }
  if (!summary && title) summary = title;

  const result: Record<string, unknown> = {
    title: title || "未获取到标题",
    summary: summary || "未获取到摘要",
  };
  if (author) {
    result.author = author;
    result.extra_info = `👤 ${author}`;
  }
  return result;
}

// ═══════ Platform Parsers ═══════

async function parseXiaohongshu(url: string): Promise<Record<string, unknown>> {
  // Extract note ID
  let noteId = "";
  let m = url.match(/xiaohongshu\.com\/(?:explore|discovery\/item)\/([a-zA-Z0-9]+)/);
  if (m) noteId = m[1];

  // Short link: follow redirect
  if (!noteId && /xhslink\.com/.test(url)) {
    try {
      const resp = await fetchWithTimeout(url);
      const finalUrl = resp.url;
      m = finalUrl.match(/xiaohongshu\.com\/(?:explore|discovery\/item)\/([a-zA-Z0-9]+)/);
      if (m) noteId = m[1];
      if (!noteId) {
        const html = await resp.text();
        return parseGenericHtml(html, finalUrl);
      }
    } catch {
      return { error: "短链接跟随失败" };
    }
  }

  if (!noteId) return { error: "无法提取笔记ID" };

  const noteUrl = `https://www.xiaohongshu.com/explore/${noteId}`;
  try {
    const resp = await fetchWithTimeout(noteUrl, {
      headers: {
        ...BROWSER_HEADERS,
        Referer: "https://www.xiaohongshu.com/",
        Origin: "https://www.xiaohongshu.com",
      },
    });
    const html = await resp.text();

    // Try __INITIAL_STATE__ — 使用贪婪匹配确保完整 JSON
    const stateMatch = html.match(
      /window\.__INITIAL_STATE__\s*=\s*({.+})\s*<\/script>/s
    );
    if (stateMatch) {
      try {
        const raw = stateMatch[1].replace(/undefined/g, "null");
        const data = JSON.parse(raw);
        const noteMap = data?.note?.noteDetailMap;
        if (noteMap) {
          const firstKey = Object.keys(noteMap)[0];
          const detail = noteMap[firstKey]?.note;
          if (detail) {
            const title = detail.title || "";
            const desc = detail.desc || "";
            const nickname = detail.user?.nickname || "";
            const likes = detail.interactInfo?.likedCount || 0;
            const comments = detail.interactInfo?.commentCount || 0;
            const collects = detail.interactInfo?.collectedCount || 0;
            const tags = (detail.tagList || []).map((t: { name?: string }) => t.name || "");
            const noteType = detail.type === "video" ? "视频" : "图文";

            // 检查是否有实质内容 — 反爬空壳数据全为空
            const hasRealContent = Boolean(title) || Boolean(desc) || Boolean(nickname);
            if (hasRealContent) {
              let summaryText = desc || title;
              if (summaryText.length > 300) summaryText = summaryText.substring(0, 300) + "...";

              return {
                title: title || "小红书笔记",
                summary: summaryText,
                author: nickname || "未知",
                likes, comments, collects, tags,
                content_type: noteType,
                extra_info: `👤 ${nickname || "未知"} · ❤️ ${likes} · 💬 ${comments} · ⭐ ${collects}`,
              };
            }
            // 空壳数据 — fall through to generic parser
          }
        }
      } catch { /* fallthrough */ }
    }

    const fallbackResult = parseGenericHtml(html, noteUrl);
    fallbackResult._fallback = true;
    return fallbackResult;
  } catch (e) {
    return { error: `请求小红书失败: ${e}` };
  }
}

async function parseYoutube(url: string): Promise<Record<string, unknown>> {
  // Extract video ID
  let videoId = "";
  const patterns = [
    /youtube\.com\/watch\?v=([a-zA-Z0-9_-]{11})/,
    /youtu\.be\/([a-zA-Z0-9_-]{11})/,
    /youtube\.com\/shorts\/([a-zA-Z0-9_-]{11})/,
  ];
  for (const p of patterns) {
    const m = url.match(p);
    if (m) { videoId = m[1]; break; }
  }
  if (!videoId) return { error: "无法提取YouTube视频ID" };

  // oembed API
  try {
    const oembed = await fetchWithTimeout(
      `https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=${videoId}&format=json`
    );
    if (oembed.ok) {
      const data = await oembed.json();
      const title = data.title || "";
      const author = data.author_name || "";

      // Get description from page
      let description = "";
      try {
        const page = await fetchWithTimeout(
          `https://www.youtube.com/watch?v=${videoId}`,
          { headers: BROWSER_HEADERS }
        );
        const html = await page.text();
        description = extractMeta(html, "og:description") || extractMeta(html, "description");
      } catch { /* ok */ }

      const summary = description || `YouTube 视频：${title}`;
      return {
        title,
        summary: summary.length > 300 ? summary.substring(0, 300) + "..." : summary,
        author,
        content_type: "视频",
        extra_info: `👤 ${author}`,
      };
    }
  } catch { /* fallthrough */ }

  // Fallback
  try {
    const resp = await fetchWithTimeout(url, { headers: BROWSER_HEADERS });
    return parseGenericHtml(await resp.text(), url);
  } catch (e) {
    return { error: `请求YouTube失败: ${e}` };
  }
}

async function parseBilibili(url: string): Promise<Record<string, unknown>> {
  let bvid = "";
  let m = url.match(/bilibili\.com\/video\/(BV[a-zA-Z0-9]+)/);
  if (m) bvid = m[1];

  // Short link
  if (!bvid && /b23\.tv/.test(url)) {
    try {
      const resp = await fetchWithTimeout(url);
      m = resp.url.match(/bilibili\.com\/video\/(BV[a-zA-Z0-9]+)/);
      if (m) bvid = m[1];
    } catch { /* */ }
  }

  if (bvid) {
    try {
      const apiResp = await fetchWithTimeout(
        `https://api.bilibili.com/x/web-interface/view?bvid=${bvid}`,
        { headers: BROWSER_HEADERS }
      );
      if (apiResp.ok) {
        const apiData = await apiResp.json();
        if (apiData.code === 0) {
          const v = apiData.data;
          const title = v.title || "";
          const desc = v.desc || "";
          const author = v.owner?.name || "";
          const view = v.stat?.view || 0;
          const like = v.stat?.like || 0;
          const coin = v.stat?.coin || 0;
          const danmaku = v.stat?.danmaku || 0;
          const fmt = (n: number) => n >= 10000 ? `${(n / 10000).toFixed(1)}万` : `${n}`;
          let summary = desc && desc !== "-" ? desc : title;
          if (summary.length > 300) summary = summary.substring(0, 300) + "...";
          return {
            title,
            summary,
            author,
            content_type: "视频",
            extra_info: `👤 ${author} · ▶️ ${fmt(view)} · 👍 ${fmt(like)} · 🪙 ${fmt(coin)} · 💬 ${fmt(danmaku)}弹幕`,
          };
        }
      }
    } catch { /* fallthrough */ }
  }

  try {
    const target = bvid ? `https://www.bilibili.com/video/${bvid}` : url;
    const resp = await fetchWithTimeout(target, { headers: BROWSER_HEADERS });
    return parseGenericHtml(await resp.text(), target);
  } catch (e) {
    return { error: `请求B站失败: ${e}` };
  }
}

async function parseZhihu(url: string): Promise<Record<string, unknown>> {
  try {
    const resp = await fetchWithTimeout(url, {
      headers: { ...BROWSER_HEADERS, Referer: "https://www.zhihu.com/" },
    });
    const html = await resp.text();
    const result = parseGenericHtml(html, url);
    result.content_type = url.includes("/p/") ? "文章" : "问答";
    return result;
  } catch (e) {
    return { error: `请求知乎失败: ${e}` };
  }
}

// ═══════ Main Handler ═══════

async function parseUrl(url: string): Promise<Record<string, unknown>> {
  const platform = identifyPlatform(url);
  let result: Record<string, unknown>;

  switch (platform.id) {
    case "xiaohongshu": result = await parseXiaohongshu(url); break;
    case "youtube": result = await parseYoutube(url); break;
    case "bilibili": result = await parseBilibili(url); break;
    case "zhihu": result = await parseZhihu(url); break;
    default: {
      try {
        const resp = await fetchWithTimeout(url, { headers: BROWSER_HEADERS });
        result = parseGenericHtml(await resp.text(), url);
      } catch (e) {
        result = { error: `请求失败: ${e}` };
      }
    }
  }

  result.platform = platform;
  result.url = url;
  return result;
}

// CORS headers
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "*",
};

Deno.serve({ port: 8900 }, async (req: Request) => {
  // CORS preflight
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  const url = new URL(req.url);

  if (url.pathname === "/" || url.pathname === "/health") {
    return new Response(
      JSON.stringify({ service: "Link Parser API", version: "1.0.0", status: "ok" }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }

  if (url.pathname === "/parse") {
    let targetUrl = url.searchParams.get("url") || "";
    if (!targetUrl) {
      return new Response(
        JSON.stringify({ error: "缺少 url 参数" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }
    if (!targetUrl.startsWith("http")) targetUrl = "https://" + targetUrl;

    try {
      const result = await parseUrl(targetUrl);
      return new Response(JSON.stringify(result), {
        headers: { ...corsHeaders, "Content-Type": "application/json" },
      });
    } catch (e) {
      return new Response(
        JSON.stringify({ error: `解析失败: ${e}`, url: targetUrl }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }
  }

  return new Response("Not Found", { status: 404, headers: corsHeaders });
});
