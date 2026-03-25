# 🔗 Link Parser API

> [随手记](https://github.com/babulchou/idea-journal) 的后端 API 服务 — 链接解析 + AI 灵感碰撞 + 智能问答

## 功能

| 接口 | 说明 |
|------|------|
| `POST /parse` | 链接解析（小红书、B站、知乎、微博、公众号等） |
| `POST /inspire` | 灵感碰撞引擎（IMA 知识库搜索 + AI 生成） |
| `POST /ask` | 回顾问答（历史记录 AI 总结 + 引用定位） |

## 技术栈

- **Python 3.12** + FastAPI + Uvicorn
- **智谱 GLM-4-Flash** (免费 AI 模型)
- **IMA OpenAPI** (知识库搜索)
- **httpx** (异步 HTTP 客户端)

## 部署

```bash
# 环境变量
export GLM_API_KEY="your-key"
export IMA_CLIENT_ID="your-id"
export IMA_API_KEY="your-key"

# 启动
pip install fastapi uvicorn httpx
uvicorn server:app --host 0.0.0.0 --port 8900
```

已部署平台：[Render](https://render.com) (免费)

## License

MIT
