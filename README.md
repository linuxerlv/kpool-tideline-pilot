# 潮汐邮差 · TIDELINE

在三片潮汐航区收取邮件，并在航程耗尽之前返回码头。潮位每两次行动变化。低潮时不能进入浅滩，等待一次也会消耗行动。用方向键、WASD 或屏幕方向按钮开船，用空格键等待。

## 本地游玩

```sh
docker build -t tideline:local .
docker run --rm -p 127.0.0.1:8080:80 tideline:local
```

打开 `http://127.0.0.1:8080/`。游戏资源由同一服务提供。

## 测试与部署

```sh
node --test tests/game.test.mjs
```

`GET /healthz` 提供容器与部署探测。Compose 使用 `KPOOL_IMAGE` 指定经过审核的镜像引用，使用 `KPOOL_SLOT_PORT` 和 `KPOOL_CONFIG_REVISION` 绑定目标端口和配置修订。正式试点可将本仓库绑定到 kpool Std 的 Compose 模板，使用仓库根目录的 Dockerfile 构建镜像。
