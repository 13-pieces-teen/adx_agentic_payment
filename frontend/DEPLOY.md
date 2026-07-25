# 前端独立部署 (DEPLOY)

前后端分离：**本目录只部署 Next.js 前端**，后端 `api + postgres + worker` 在队友服务器
（`api.arena402.com` / `42.193.162.10`）运行。前端所有数据请求通过 `NEXT_PUBLIC_API_URL` 打到后端。

## 域名结构（目标）
| 地址 | 用途 | 部署位置 |
|---|---|---|
| `https://www.arena402.com` | 正式前端 | 本 docker（或你选的托管） |
| `https://arena402.com` | 308 跳转到 www | 同上 |
| `https://api.arena402.com` | REST / WSS | 队友服务器 42.193.162.10 |
| PostgreSQL 5432 | 数据库 | 仅队友服务器 Docker 内网 |

> Vercel 导不进团队私有 repo，所以前端走自建 docker 部署，不用 Vercel。

## 关键点：NEXT_PUBLIC_API_URL 是 build 期变量
Next.js 把 `NEXT_PUBLIC_*` 在**构建时**内联进产物。所以改 API 地址必须**重新 build 镜像**，
运行时改环境变量无效。Dockerfile / compose 已通过 build arg 处理好。

## 用 docker compose 部署（推荐）
```bash
cd frontend
NEXT_PUBLIC_API_URL=https://api.arena402.com docker compose up -d --build
# 访问 http://<本机>:3000
```
改端口：`WEB_PORT=8080 NEXT_PUBLIC_API_URL=... docker compose up -d --build`

## 只用 Dockerfile（不用 compose）
```bash
cd frontend
docker build --build-arg NEXT_PUBLIC_API_URL=https://api.arena402.com -t arena402-web .
docker run -d -p 3000:3000 arena402-web
```

## 不用 docker，直接 node 部署
```bash
cd frontend
npm ci
NEXT_PUBLIC_API_URL=https://api.arena402.com npm run build
NEXT_PUBLIC_API_URL=https://api.arena402.com npm run start   # 监听 :3000
```

## HTTPS / 域名
本 compose 只暴露 web:3000，未含 TLS。生产上加 HTTPS 两种方式：
1. 前面套一层 Caddy/Nginx 反代 + Let's Encrypt（团队 repo 根 `docker-compose.production.yml` 有 caddy 服务可参考，但那是全栈版）。
2. 或把 `www.arena402.com` DNS 指向本机，用系统级 Caddy 自动签证书反代到 :3000。

## 已验证
- `npm ci` + `npm run build`：通过（Next.js 15.5，6 页面全部编译）。
- Dockerfile builder 阶段逻辑等价于上述 build 命令。
- ⚠️ 本机未安装 docker CLI，**镜像 build 未在此机实测**；逻辑与团队 repo `deploy/docker/Dockerfile.web` 一致，请在有 docker 的机器上首次 build 验证。
